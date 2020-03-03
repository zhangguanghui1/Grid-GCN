"""
PointNet++ model for ModelNet40 classification and ScanNet semantic labelling
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '../..'))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
# import ctypes
# _ = ctypes.CDLL(os.path.join(os.path.dirname(os.path.abspath(__file__)),'../../gridifyop/additional.so'))
# print(os.path.join(os.path.dirname(os.path.abspath(__file__)),'../../gridifyop/additional.so'))
import mxnet as mx
from configs.configs import configs
from models.gcn_module_g_att import sub_g_update
from utils.ops import batch_take_g, batch_take_c, conv1d, batch_take,  mlp1d_c, mlp2d_c
import custom_op.weighted_gradient
from mxnet.symbol import Gridify
from mxnet.symbol import GridifyUp

BN = False if (configs["use_bn"] == 'f' or configs["use_bn"] == 'p') else True
C_dim = 1 if BN else 2
CLS_NUM=16
SEG_NUM_CLS = 50
def get_seg_inputs():
    data = mx.symbol.Variable(name='data')
    actual_centnum = mx.symbol.Variable(name='actual_centnum')
    cls = mx.symbol.Variable(name='cls')
    label = mx.symbol.Variable(name='label')
    return data, actual_centnum, cls, label

def get_seg_head(features, label, dropout_ratio=0.5, bn_decay=0.9, weights=None):
    """
    Get symbol for ScanNet semantic labelling
    """
    net = conv1d(features, num_filter=128, kernel=(1,), stride=(1,), bn_decay=bn_decay, layout='NCW', scope='fc1')
    if dropout_ratio > 0:
        net = mx.symbol.Dropout(net, p=dropout_ratio, name='fc1/dropout')
    net = conv1d(net, num_filter=SEG_NUM_CLS, kernel=(1,), stride=(1,), layout='NCW', use_bn=False, use_relu=False, scope='fc2')
    if weights is not None:
        print("weights is not None")
        net = mx.symbol.Custom(data=net, weight=weights, input_dim=3, name='fc2/weighted', op_type='weighted_gradient')
    net = mx.symbol.SoftmaxOutput(data=net, label=label, use_ignore=False, multi_output=True, normalization='valid', name='pred')
    # return net
    return net

def debug_get_seg_head(features, label, dropout_ratio=0.5, bn_decay=0.9, weights=None):
    """
    Get symbol for ScanNet semantic labelling
    """
    net = conv1d(features, num_filter=128, kernel=(1,), stride=(1,), bn_decay=bn_decay, layout='NCW', scope='fc1')
    if dropout_ratio > 0:
        net = mx.symbol.Dropout(net, p=dropout_ratio, name='fc1/dropout')
    num_net = conv1d(net, num_filter=21, kernel=(1,), stride=(1,), layout='NCW', use_bn=False, use_relu=False, scope='fc2')
    if weights is not None:
        print("weights is not None")
        num_net = mx.symbol.Custom(data=num_net, weight=weights, input_dim=3, name='fc2/weighted', op_type='weighted_gradient')
    net = mx.symbol.SoftmaxOutput(data=num_net, label=label, use_ignore=True, ignore_label=0, multi_output=True, normalization='valid', name='pred')
    # return net
    return mx.symbol.Group([net, num_net, features])

def neighbors_fetch(downdata, downnum, updata, upnum, shape, k=3, scope=''):
    """
    k nearest neighbor
    downdata: symbol, shape=(B, N, 3)
    downnum: symbol, shape=(B, 1)
    centers: symbol, shape=(B, O, 3)
    upnum: symbol, shape=(B, 1)
    shape: specify (M, N)
    scope: name scope for this operator
    returns:
        dist, shape=(B, M, k), squared distances to the k nearest points
        ids, shape=(B, M, k), dtype=int32, indices of the k nearest points
    """


    if k == 3:
        # query: (B, M, 3), Symbol, locations to interpolate
        # data: (B, N, 3), Symbol, locations of existing points

        _, nebidx = mx.sym.contrib.ThreeNN(updata, downdata)  # (B, M, k)
    else:

        nebidx = mx.sym.contrib.KNN(updata, downdata, downnum, upnum, k = k)  # (B, M, k)

    return nebidx, None

def knn(query, data, shape, k=3, scope=''):
    """
    k nearest neighbor
    query: symbol, shape=(B, M, 3), query points
    data: symbol, shape=(B, N, 3), all points
    shape: specify (M, N)
    scope: name scope for this operator
    returns:
        dist, shape=(B, M, k), squared distances to the k nearest points
        ids, shape=(B, M, k), dtype=int32, indices of the k nearest points
    """
    M, N = shape
    query = query.expand_dims(axis=2)  # (B, M, 1, 3)
    data = data.expand_dims(axis=1)  # (B, 1, N, 3)
    diff = mx.symbol.broadcast_sub(query, data)  # (B, M, N, 3)
    all_dist = mx.symbol.sum(diff*diff, axis=3)  # (B, M, N)
    ids = mx.symbol.topk(all_dist, axis=2, k=k, ret_typ='indices', is_ascend=True, name='{}_top{}'.format(scope, k))   # (B, M, k)
    return ids.astype('int32')


def get_symbol_seg_ggcn(B, N, take_shapes, take_up_shapes, bn_decay=0.9, weights=None):
    """
    Get symbol for GGCN ModelNet40 classification
    B: batch_size
    N: #points in each batch
    weights: weights for weighted_gradient
    The model has inputs:
      data: (B, N, 3), point clouds
      'neighbors_arr' + str(i), (self.batch_size, self.max_o_grid[i], self.max_p_grid[i])
      'neighbors_mask' + str(i), (self.batch_size, self.max_o_grid[i], self.max_p_grid[i])
      'centers_arr' + str(i), (self.batch_size, self.max_o_grid[i], 3)),
      'centers_mask' + str(i), (self.batch_size, self.max_o_grid[i]
      label: (B,), ground truth labels
    returns: symbol, (B, 40)
    """
    # inputs
    data, actual_centnum, cls, label = get_seg_inputs()
    # get features and sub GCN
    center_locnfeat_alllayers = []
    up_center_locnfeat_alllayers = []
    pt_mlp_lsts = configs["pt_ele_dim"]
    max_o_grid_lst = configs["max_o_grid_lst"]
    up_max_p_grid_lst = configs["up_max_p_grid_lst"]
    centers_xyz_reverse_lst = [data]
    if BN:
        centers_xyz_bn_reverse_lst = [data.transpose(axes=(0,2,1), name="dataxyz_tpose")]
        centers_den_bn_reverse_lst = [mx.sym.ones((B, 1, N), dtype="float32")]
        data = mx.sym.concat(data, mx.sym.ones((B, N, 1),dtype="float32"), dim = 2)
    else:
        centers_xyz_bn_reverse_lst = [data]
        centers_den_bn_reverse_lst = [mx.sym.ones((B, N, 1),dtype="float32")]
        data = mx.sym.concat(data, centers_den_bn_reverse_lst[0], dim=2)
    data_layer = data
    centers_reverse_lst = [data]
    centers_mask_lst = []
    centnum_lst = [actual_centnum]
    if configs["featxyz"] is not None :
        data_feat = mx.sym.slice_axis(data, axis=2, begin=0, end=3);
    else:
        data_feat = data
    if BN:
        data_trans = data_feat.transpose(axes=(0, 2, 1), name="up_center_tpose")
        center_locnfeat_alllayers.append(data_trans)
    else:
        center_locnfeat_alllayers.append(data_feat)
    data_loc = data
    for i in range(len(max_o_grid_lst)):
        take_shapes[i][-1]+=4
        nebidx, nebidxmsk, centers, centmsk, actual_centnum \
            = Gridify(data_loc, actual_centnum, max_o_grid=max_o_grid_lst[i],
                max_p_grid=configs["max_p_grid_lst"][i],
                kernel_size=configs["kernel_size_lst"][i], stride=configs['stride_lst'][i],
                coord_shift=configs["lidar_coord"], voxel_size=configs['voxel_size_lst'][i],
                grid_size=configs['grid_size_lst'][i], loc=1 if configs["loc_within"] else 0)
        data_loc = centers
        centers_xyz = mx.sym.slice_axis(centers, axis=2, begin=0, end=3)
        centers_den = mx.sym.slice_axis(centers, axis=2, begin=3, end=4)
        centers_xyz_reverse_lst.append(centers_xyz)
        centers_reverse_lst.append(data_loc)
        centers_mask_lst.append(centmsk)
        centnum_lst.append(actual_centnum)

        if BN:
            if i > 0:
                data_layer = data_layer.transpose(axes=(0, 2, 1),
                                      name= "/down_batch_take_g_pre_tpose{}".format(i))
            neighbors = batch_take_g(data_layer, nebidx,
                            take_shapes[i], scope='down_neigh_batch_take_'+str(i))
            neighbors = neighbors.transpose(axes=(0, 3, 1, 2),
                            name="/down_batch_take_g_post_tpose{}".format(i))
            centers = centers.transpose(axes=(0,2,1), name="center_tpose"+str(i))
            centers_xyz = centers_xyz.transpose(axes=(0,2,1), name="center_xyz_tpose"+str(i))
            centers_den = centers_den.transpose(axes=(0,2,1), name="center_den_tpose"+str(i))
        else:
            neighbors = batch_take_g(data_layer, nebidx, take_shapes[i], scope='down_neigh_batch_take_' + str(i))
        centers_xyz_bn_reverse_lst.append(centers_xyz)
        centers_den_bn_reverse_lst.append(centers_den)

        pt_mlp_lst = pt_mlp_lsts[i] if pt_mlp_lsts is not None else None
        center_feats = sub_g_update(centers_xyz, centers_den, neighbors, i>0, centmsk, nebidxmsk, configs["attfdim"], pt_mlp_lst=pt_mlp_lst, outDim=configs['gcn_outDim'][i], cntxt_mlp= configs["cntxt_mlp_lst"][i] if configs["cntxt_mlp_lst"] is not None else None, shape = take_shapes[i], scope='sub_g_'+str(i), aggtype=configs["aggtype"], pool_type=configs["agg"], att_full= configs["att_full"], recalden = False, bn_decay=bn_decay, att_norm=configs["att_norm"] if "att_norm" in configs else None)
        data_layer = mx.sym.concat(centers, center_feats, dim=C_dim, name="down_concat_datalayer"+str(i))

        if configs["featxyz"] == "feat" and i!= len(max_o_grid_lst)-1:
            center_locnfeat_alllayers.append(center_feats)
        elif configs["featxyz"] == "featxyz" and i!= len(max_o_grid_lst)-1:
            center_locnfeat_alllayers.append(mx.sym.concat(centers_xyz, center_feats, dim=C_dim, name="extractfeatxyz"+str(i)))
        else:
            center_locnfeat_alllayers.append(data_layer)

    # up sampling part
    f_last_layer = center_locnfeat_alllayers[-1]
    for i in range(len(up_max_p_grid_lst)):
        take_up_shapes[i][-1] = take_up_shapes[i][-1] + 4
        downdata = centers_reverse_lst[-i-1]
        downdata_xyz = centers_xyz_reverse_lst[-i-1]
        centers = centers_reverse_lst[-i-2]
        updata_xyz = centers_xyz_reverse_lst[-i-2]
        centers_xyz = centers_xyz_bn_reverse_lst[-i-2]
        centers_den = centers_den_bn_reverse_lst[-i-2]
        downnum = centnum_lst[-i-1]
        upnum = centnum_lst[-i-2]


        if configs["multi"]:
            nebidx1, nebidxmsk1 = \
                neighbors_fetch(downdata_xyz, downnum, updata_xyz, upnum,
                    (configs["up_max_o_grid_lst"][i], configs["max_o_grid_lst"][-i-1]), k=3, scope='up_{}/up_neigh_fetch'.format(i))

            nebidx2, nebidxmsk2 = \
                GridifyUp(downdata, centers, downnum, upnum, max_p_grid=configs["up_max_p_grid_lst"][i],
                max_o_grid=configs["up_max_o_grid_lst"][i], kernel_size=configs["up_kernel_size_lst"][i],
                coord_shift=configs["lidar_coord"], voxel_size=configs['up_voxel_size_lst'][i],
                grid_size=configs['up_grid_size_lst'][i])

            nebidx = mx.sym.concat(nebidx1,nebidx2, dim=2)
            nebidxmsk = mx.sym.concat(mx.sym.ones((B, configs["up_max_o_grid_lst"][i], 3), dtype="float32"), nebidxmsk2, dim=2)

        elif configs["up_neigh_fetch"]:
            nebidx, nebidxmsk = \
                neighbors_fetch(downdata_xyz, downnum, updata_xyz, upnum,
                    (configs["up_max_o_grid_lst"][i], configs["max_o_grid_lst"][-i-1]), k=configs["up_max_p_grid_lst"][i], scope='up_{}/up_neigh_fetch'.format(i))
        else:
            nebidx, nebidxmsk = \
                GridifyUp(downdata, centers, downnum, upnum, max_p_grid=configs["up_max_p_grid_lst"][i],
                max_o_grid=configs["up_max_o_grid_lst"][i], kernel_size=configs["up_kernel_size_lst"][i],
                coord_shift=configs["lidar_coord"], voxel_size=configs['up_voxel_size_lst'][i],
                grid_size=configs['up_grid_size_lst'][i])


        f_this_layer = center_locnfeat_alllayers[-i - 2]
        if BN:
            f_last_layer = f_last_layer.transpose(axes=(0, 2, 1),
                                  name="/up_batch_take_g_pre_tpose{}".format(i))

            neighbors_up = batch_take_g(f_last_layer, nebidx, take_up_shapes[i],
                                        scope='up_neigh_up_batch_take_g_' + str(i))

            neighbors_up = neighbors_up.transpose(axes=(0, 3, 1, 2),
                            name="/up_batch_take_g_post_tpose{}".format(i))
        else:
            neighbors_up = batch_take_g(f_last_layer, nebidx, take_up_shapes[i], scope='up_neigh_up_batch_take_g' + str(i))
        center_masks = centers_mask_lst[-i-2] if i != len(configs['up_gcn_outDim'])-1 else None
        up_pt_mlp_lst = configs["up_pt_ele_dim"][i] if configs["up_pt_ele_dim"] is not None else None
        print("uplayer {}, take_up_shapes: {}".format(i, take_up_shapes[i]))
        if BN:
            centers = centers.transpose(axes=(0, 2, 1), name="center_tpose" + str(i))
        center_feats_up = sub_g_update(centers_xyz, centers_den, neighbors_up, True, center_masks, nebidxmsk, configs["up_attfdim"], center_ori_feats = f_this_layer, pt_mlp_lst=up_pt_mlp_lst, outDim=configs['up_gcn_outDim'][i], cntxt_mlp= configs["up_cntxt_mlp_lst"][i] if configs["up_cntxt_mlp_lst"] is not None else None, shape = take_up_shapes[i], scope='up_sub_g_'+str(i), aggtype=configs["up_aggtype"], pool_type=configs["up_agg"],att_full= configs["up_att_full"], center_dim=configs["up_center_dim"][i], recalden = True, bn_decay=bn_decay, att_norm=configs["up_att_norm"] if "up_att_norm" in configs else None)
        up_center_locnfeat_alllayers.append(center_feats_up)
        f_last_layer = mx.sym.concat(centers, center_feats_up, dim=C_dim, name="up_concat_f_last_layer"+str(i))
    # segmentation head
    f_last_layer = up_center_locnfeat_alllayers[-1] # noloc
    if configs["xyz_last"]:
        f_last_layer = mx.sym.concat(f_last_layer, centers_xyz, dim=C_dim, name="xyz_last_concat")
    if not BN:
        f_last_layer = f_last_layer.transpose(axes=(0, 2, 1), name="last_f_transpose")
    if configs["skip"]: 
        print("skip feature added")
        if BN:
            last_centers_feat = mx.sym.slice_axis(center_locnfeat_alllayers[-1], axis=1, begin=4, end=None)
            kernel_size = configs["max_o_grid_lst"][-1]
            last_centers_feat = mx.sym.Pooling(name="lastcent/max_pooling", data=last_centers_feat, kernel=(kernel_size), pool_type="max", layout="NCW")
            last_centers_feat = mlp1d_c(last_centers_feat, [128], use_bn=BN, bn_decay=bn_decay, scope="skipcent_mlp1d_c")
            last_centers_feat = mx.sym.tile(last_centers_feat, reps=(1, 1, configs["up_max_o_grid_lst"][-1])) 
            f_last_layer = mx.sym.concat(f_last_layer, last_centers_feat, dim=1, name="skipcenter_concat")
        else:
            print('errorrrrrrrrr!')
    if configs["clsty"] is not None:
        print("clsty not none")
        cls = mx.sym.tile(mx.sym.expand_dims(cls, axis=2, name="clsexpand"), reps=(1, 1, N))
        f_last_layer = mx.sym.concat(f_last_layer, cls, dim=1, name="beforefinal_concat")    
        if configs["clsty"] == "concat": 
            cls = mlp1d_c(cls, [64], use_bn=BN, bn_decay=bn_decay, scope="skip_cls_mlp1d_c")
            f_last_layer = mx.sym.concat(f_last_layer, cls, dim=1, name="beforefinal_concat")
        elif configs["clsty"] == "mult":
            chan = configs["up_gcn_outDim"][-1][-1] + 16
            if configs["skip"]: chan+=128
            if configs["xyz_last"]: chan+=3
            cls = mlp1d_c(cls, [chan], use_bn=BN, bn_decay=bn_decay, scope="skip_cls_mlp1d_c")
            f_last_layer = mx.sym.elemwise_mul(f_last_layer, cls)
    else:
        f_last_layer = mx.sym.broadcast_add(f_last_layer, mx.sym.min(cls, axis = 1, keepdims=True).expand_dims(axis=1))
    
    net = get_seg_head(f_last_layer, label, bn_decay=bn_decay, weights=weights)
    return net

if __name__ == "__main__":
    '''
    #net = get_symbol_cls_ssg(B=64, N=1024)
    net = get_symbol_seg(B=64, N=1024)
    t = mx.viz.plot_network(net)
    t.render()
    '''

    # test pointnet_fp_module
    context = mx.cpu()
    data_data = []
    for i in range(3):
        for j in range(3):
            for k in range(3):
                data_data.append([i,j,k])
    data_data = mx.nd.array(data_data)
    data_data = mx.nd.stack(data_data, data_data*2, axis=0)  # (2, 27, 3)
    query_data = mx.nd.array([[[0.5, 0.5, 0.5]],[[0.5,0.5,0.5]]])  # (2, 1, 3)
    feature_data = mx.nd.arange(27).reshape((27, 1))
    feature_data = mx.nd.stack(feature_data, feature_data, axis=0)
    query = mx.symbol.Variable('query')
    data = mx.symbol.Variable('data')
    feature = mx.symbol.Variable('feature')
    output = pointnet_fp_module(data, feature, query, None, (2,1,27,1), mlp_list=[])
    mod = mx.mod.Module(output, data_names=['query', 'data', 'feature'], label_names=[], context=context)
    mod.bind(data_shapes=[('query', query_data.shape), ('data', data_data.shape), ('feature', feature_data.shape)])
    mod.init_params()
    mod.forward(mx.io.DataBatch([query_data, data_data, feature_data]))
    print(mod.get_outputs()[0].asnumpy())  # [[[4/3]], [[12/17]]]

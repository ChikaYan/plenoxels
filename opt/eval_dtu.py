# Script modified from https://github.com/jzhangbs/DTUeval-python

from genericpath import isdir
import numpy as np
import open3d as o3d
import sklearn.neighbors as skln
from tqdm import tqdm
from scipy.io import loadmat
import multiprocessing as mp
import argparse
import os
from torch.utils.tensorboard import SummaryWriter
import json
from util import config_util
from pathlib import Path

def sample_single_tri(input_):
    n1, n2, v1, v2, tri_vert = input_
    c = np.mgrid[:n1+1, :n2+1]
    c += 0.5
    c[0] /= max(n1, 1e-7)
    c[1] /= max(n2, 1e-7)
    c = np.transpose(c, (1,2,0))
    k = c[c.sum(axis=-1) < 1]  # m2
    q = v1 * k[:,:1] + v2 * k[:,1:] + tri_vert
    return q

def write_vis_pcd(file, points, colors):
    pcd = o3d.geometry.PointCloud()
    pcd.points = o3d.utility.Vector3dVector(points)
    pcd.colors = o3d.utility.Vector3dVector(colors)
    o3d.io.write_point_cloud(file, pcd)

def sample_mesh(data_mesh, thresh, nn_engine):
    vertices = np.asarray(data_mesh.vertices)
    triangles = np.asarray(data_mesh.triangles)
    tri_vert = vertices[triangles]
    v1 = tri_vert[:,1] - tri_vert[:,0]
    v2 = tri_vert[:,2] - tri_vert[:,0]
    l1 = np.linalg.norm(v1, axis=-1, keepdims=True)
    l2 = np.linalg.norm(v2, axis=-1, keepdims=True)
    area2 = np.linalg.norm(np.cross(v1, v2), axis=-1, keepdims=True)
    non_zero_area = (area2 > 0)[:,0]
    l1, l2, area2, v1, v2, tri_vert = [
        arr[non_zero_area] for arr in [l1, l2, area2, v1, v2, tri_vert]
    ]
    thr = thresh * np.sqrt(l1 * l2 / area2)
    n1 = np.floor(l1 / thr)
    n2 = np.floor(l2 / thr)

    with mp.Pool() as mp_pool:
        new_pts = mp_pool.map(sample_single_tri, ((n1[i,0], n2[i,0], v1[i:i+1], v2[i:i+1], tri_vert[i:i+1,0]) for i in range(len(n1))), chunksize=1024)


    # print("[v1, v2, l1, l2]")
    # print([v1, v2, l1, l2])

    # print("area2")
    # print(area2)
    # print("[n1,n2]")
    # print([n1,n2])

    # print("thr")
    # print(thr)
    # print("new_pts")
    # print(new_pts)
    new_pts = np.concatenate(new_pts, axis=0)
    data_pcd = np.concatenate([vertices, new_pts], axis=0)

    # downsample
    nn_engine.fit(data_pcd)
    rnn_idxs = nn_engine.radius_neighbors(data_pcd, radius=thresh, return_distance=False)
    mask = np.ones(data_pcd.shape[0], dtype=np.bool_)
    for curr, idxs in enumerate(rnn_idxs):
        if mask[curr]:
            mask[idxs] = 0
            mask[curr] = 1
    data_pcd = data_pcd[mask]
    return data_pcd

if __name__ == '__main__':
    mp.freeze_support()

    parser = argparse.ArgumentParser()
    parser.add_argument('--input_path', type=str, default=None)
    parser.add_argument('--scan', type=int, default=1)
    parser.add_argument('--dataset_dir', type=str, default='/home/tw554/plenoxels/data/dtu_eval/SampleSet/MVS Data')
    parser.add_argument('--downsample_density', type=float, default=0.2)
    parser.add_argument('--patch_size', type=float, default=60)
    parser.add_argument('--max_dist', type=float, default=20)
    parser.add_argument('--visualize_threshold', type=float, default=10)
    parser.add_argument('--run_alpha_shape', action='store_true', default=False,
                        help='convert point to mesh, then eval cf')
    parser.add_argument('--alpha_shape_alpha', type=float, default=0.5)
    parser.add_argument('--out_dir', type=str, default=None)
    # parser.add_argument('--del_ckpt', action='store_true', default=False)
    parser.add_argument('--no_pts_save', action='store_true', default=False)
    parser.add_argument('--log_tune_hparam_config_path', type=str, default=None,
                       help='Log hyperparamters being tuned to tensorboard based on givn config.json path')
    parser.add_argument('--hparam_save_name', type=str, default='hparam_pts')
    args = parser.parse_args()

    thresh = args.downsample_density
    nn_engine = skln.NearestNeighbors(n_neighbors=1, radius=thresh, algorithm='kd_tree', n_jobs=-1)

    pbar = tqdm(total=8)
    pbar.set_description('read data pcd')
    if os.path.isdir(args.input_path):
        data_pcd = np.load(f'{args.input_path}/pts.npy')
    else:
        if args.input_path.endswith('.obj') or args.input_path.endswith('.ply'):
            # read from mesh
            data_mesh = o3d.io.read_triangle_mesh(args.input_path)
            data_pcd = sample_mesh(data_mesh, thresh, nn_engine)
        else:
            data_pcd = np.load(args.input_path)

    summary_writer = SummaryWriter(f'{os.path.dirname(args.input_path)}/../')

    if args.run_alpha_shape:
        pcd = o3d.geometry.PointCloud()
        pcd.points = o3d.utility.Vector3dVector(data_pcd)
        print('running alpha shape')
        data_mesh = o3d.geometry.TriangleMesh.create_from_point_cloud_alpha_shape(pcd, args.alpha_shape_alpha)
        print('alpha shape finished')

        os.makedirs(args.out_dir, exist_ok=True)
        o3d.io.write_triangle_mesh(f'{args.out_dir}/mesh_{args.alpha_shape_alpha}.ply', data_mesh)

        data_pcd = sample_mesh(data_mesh, thresh, nn_engine)    


    pbar.update(1)
    pbar.set_description('random shuffle pcd index')
    shuffle_rng = np.random.default_rng()
    shuffle_rng.shuffle(data_pcd, axis=0)

    pbar.update(1)
    pbar.set_description('downsample pcd')
    # nn_engine.fit(data_pcd)
    # rnn_idxs = nn_engine.radius_neighbors(data_pcd, radius=thresh, return_distance=False)
    # mask = np.ones(data_pcd.shape[0], dtype=np.bool_)
    # for curr, idxs in enumerate(rnn_idxs):
    #     if mask[curr]:
    #         mask[idxs] = 0
    #         mask[curr] = 1
    # data_down = data_pcd[mask]
    data_down = data_pcd

    pbar.update(1)
    pbar.set_description('masking data pcd')
    obs_mask_file = loadmat(f'{args.dataset_dir}/ObsMask/ObsMask{args.scan}_10.mat')
    ObsMask, BB, Res = [obs_mask_file[attr] for attr in ['ObsMask', 'BB', 'Res']]
    BB = BB.astype(np.float32)

    patch = args.patch_size
    inbound = ((data_down >= BB[:1]-patch) & (data_down < BB[1:]+patch*2)).sum(axis=-1) ==3
    data_in = data_down[inbound]

    data_grid = np.around((data_in - BB[:1]) / Res).astype(np.int32)
    grid_inbound = ((data_grid >= 0) & (data_grid < np.expand_dims(ObsMask.shape, 0))).sum(axis=-1) ==3
    data_grid_in = data_grid[grid_inbound]
    in_obs = ObsMask[data_grid_in[:,0], data_grid_in[:,1], data_grid_in[:,2]].astype(np.bool_)
    data_in_obs = data_in[grid_inbound][in_obs]

    pbar.update(1)
    pbar.set_description('read STL pcd')
    stl_pcd = o3d.io.read_point_cloud(f'{args.dataset_dir}/Points/stl/stl{args.scan:03}_total.ply')
    stl = np.asarray(stl_pcd.points)

    pbar.update(1)
    pbar.set_description('compute data2stl')
    nn_engine.fit(stl)
    dist_d2s, idx_d2s = nn_engine.kneighbors(data_in_obs, n_neighbors=1, return_distance=True)
    max_dist = args.max_dist
    mean_d2s = dist_d2s[dist_d2s < max_dist].mean()

    pbar.update(1)
    pbar.set_description('compute stl2data')
    ground_plane = loadmat(f'{args.dataset_dir}/ObsMask/Plane{args.scan}.mat')['P']

    stl_hom = np.concatenate([stl, np.ones_like(stl[:,:1])], -1)
    above = (ground_plane.reshape((1,4)) * stl_hom).sum(-1) > 0
    stl_above = stl[above]

    nn_engine.fit(data_in)
    dist_s2d, idx_s2d = nn_engine.kneighbors(stl_above, n_neighbors=1, return_distance=True)
    mean_s2d = dist_s2d[dist_s2d < max_dist].mean()

    # accuracy = np.count_nonzero(dist_d2s < args.f1_dist) / len(dist_d2s)
    # completeness = np.count_nonzero(dist_s2d < args.f1_dist) / len(dist_s2d)
    # f1 = 2. / (1./np.clip(accuracy, 1e-10, None) + 1./ np.clip(completeness, 1e-10, None))


    pbar.update(1)
    pbar.set_description('visualize error')
    vis_dist = args.visualize_threshold
    R = np.array([[1,0,0]], dtype=np.float64)
    G = np.array([[0,1,0]], dtype=np.float64)
    B = np.array([[0,0,1]], dtype=np.float64)
    W = np.array([[1,1,1]], dtype=np.float64)
    data_color = np.tile(B, (data_down.shape[0], 1))
    data_alpha = dist_d2s.clip(max=vis_dist) / vis_dist
    data_color[ np.where(inbound)[0][grid_inbound][in_obs] ] = R * data_alpha + W * (1-data_alpha)
    data_color[ np.where(inbound)[0][grid_inbound][in_obs][dist_d2s[:,0] >= max_dist] ] = G
    stl_color = np.tile(B, (stl.shape[0], 1))
    stl_alpha = dist_s2d.clip(max=vis_dist) / vis_dist
    stl_color[ np.where(above)[0] ] = R * stl_alpha + W * (1-stl_alpha)
    stl_color[ np.where(above)[0][dist_s2d[:,0] >= max_dist] ] = G
    pbar.update(1)
    pbar.set_description('done')
    pbar.close()
    over_all = (mean_d2s + mean_s2d) / 2
    print(mean_d2s, mean_s2d, over_all)

    # read image eval metrics if avaliable
    img_eval_path = Path(args.input_path).parent / '..' / 'render_eval.txt'
    if not img_eval_path.exists():
        img_eval_path = Path(args.input_path).parent / '..' / '..' / 'render_eval.txt'

    if img_eval_path.exists():
        with img_eval_path.open('r') as f:
            psnr = float(f.readline().split(':')[-1].strip())
    else:
        psnr = None

    print(f'======= eval result =======')
    print(f'Mean d2s: {mean_d2s}')
    print(f'Mean s2d: {mean_s2d}')
    print(f'Avg cf: {over_all}')
    print(f'psnr: {psnr}\n')

    if args.out_dir is not None:
        os.makedirs(args.out_dir, exist_ok=True)
        if not args.no_pts_save:
            write_vis_pcd(f'{args.out_dir}/vis_{args.scan:03}_d2s.ply', data_down, data_color)
            write_vis_pcd(f'{args.out_dir}/vis_{args.scan:03}_s2d.ply', stl, stl_color)


        with open(f'{args.out_dir}/cf.txt', 'w') as f:
            f.write(f'Mean d2s: {mean_d2s}\n')
            f.write(f'Mean s2d: {mean_s2d}\n')
            f.write(f'Avg cf: {over_all}\n')

            f.write(f'psnr: {psnr}\n')


    # log hparams for tuning tasks
    if args.log_tune_hparam_config_path is not None:
        train_args = config_util.setup_train_conf(return_parpser=True).parse_known_args(
            args=['-c', f'{os.path.dirname(args.input_path)}/../config.yaml',
            '--data_dir', 'foo']
            )[0]
        with open(args.log_tune_hparam_config_path, 'r') as f:
            tune_conf = json.load(f)
        hparams = {}
        for hp in tune_conf['params']:
            arg = hp['text'].split('=')[0].strip()
            value = getattr(train_args, arg)
            if type(value) is list:
                hparams[arg] = str(value)
            else:
                hparams[arg] = value
        
        metrics = {
            'Chamfer/d2s': mean_d2s,
            'Chamfer/s2d': mean_s2d,
            'Chamfer/mean': over_all,
        }
        if psnr is not None:
            metrics['Image/psnr'] = psnr
        summary_writer.add_hparams(hparams, metrics, run_name=args.hparam_save_name)
        summary_writer.flush()
    else:
        summary_writer.add_scalar('Chamfer/d2s', mean_d2s, global_step=0)
        summary_writer.add_scalar('Chamfer/s2d', mean_s2d, global_step=0)
        summary_writer.add_scalar('Chamfer/mean', over_all, global_step=0)
        if psnr is not None:
            summary_writer.add_scalar('Image/psnr', psnr, global_step=0)
        summary_writer.flush()
        summary_writer.close()

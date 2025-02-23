# Copyright 2021 Alex Yu

# First, install svox2
# Then, python opt.py <path_to>/nerf_synthetic/<scene> -t ckpt/<some_name>
# or use launching script:   sh launch.sh <EXP_NAME> <GPU> <DATA_DIR>
import torch
import torch.cuda
import torch.optim
import torch.nn.functional as F
import svox2
import open3d as o3d

# import sys
# from os import path
# sys.path.append( path.dirname( path.dirname( path.abspath(__file__) ) ) )
# import svox2

from pathlib import Path

import json
import imageio
import os
from os import path
import shutil
import gc
import numpy as np
import math
import cv2
from util.dataset import datasets
from util.util import Timing, get_expon_lr_func, generate_dirs_equirect, viridis_cmap, get_linear_lr_func, pose_spherical
from util import config_util
import ast
import sklearn.neighbors as skln
from eval_cf_blender import eval_cf, write_vis_pcd

from warnings import warn
from datetime import datetime
from torch.utils.tensorboard import SummaryWriter

from tqdm import tqdm
from typing import NamedTuple, Optional, Union

# os.environ['CUDA_LAUNCH_BLOCKING'] = '1'

device = "cuda" if torch.cuda.is_available() else "cpu"

# parser = argparse.ArgumentParser()
# parser.add_argument('--train_dir', '-t', type=str, default='ckpt',
#                     help='checkpoint and logging directory')
# parser.add_argument('--gin-config', '-c',
#                         type=str,
#                         default=None,
#                         help="Config yaml file (will override args)")


# FLAGS = parser.parse_args()
# args = config_util.setup_train_conf(FLAGS)

args = config_util.setup_train_conf()
USE_KERNEL = not args.nokernel
if args.surface_type is None:
    args.surface_type = 'none'

assert args.lr_sigma_final <= args.lr_sigma, "lr_sigma must be >= lr_sigma_final"
assert args.lr_alpha_final <= args.lr_alpha, "lr_alpha must be >= lr_alpha_final"
assert args.lr_sh_final <= args.lr_sh, "lr_sh must be >= lr_sh_final"
assert args.lr_basis_final <= args.lr_basis, "lr_basis must be >= lr_basis_final"

os.makedirs(args.train_dir, exist_ok=True)
summary_writer = SummaryWriter(args.train_dir)

# reso_list = json.loads(args.reso)
reso_list = [ast.literal_eval(args.reso[i]) for i in range(len(args.reso))]
reso_id = 0

# with open(path.join(args.train_dir, 'args.json'), 'w') as f:
#     json.dump(args.__dict__, f, indent=2)
    # # Changed name to prevent errors
    # shutil.copyfile(__file__, path.join(args.train_dir, 'opt_frozen.py'))

with open(path.join(args.train_dir, 'args.yaml'), 'w') as file:
    for arg in sorted(vars(args)):
        attr = getattr(args, arg)
        file.write('{} = {}\n'.format(arg, attr))
if args.config != path.join(args.train_dir, 'config.yaml'):
    shutil.copyfile(args.config, path.join(args.train_dir, 'config.yaml'))

torch.manual_seed(20200823)
np.random.seed(20200823)

factor = 1
dset = datasets[args.dataset_type](
               args.data_dir,
               split="train",
               device=device,
               factor=factor,
               n_images=args.n_train,
               **config_util.build_data_options(args))

if args.background_nlayers > 0 and not dset.should_use_background:
    warn('Using a background model for dataset type ' + str(type(dset)) + ' which typically does not use background')

dset_test = datasets[args.dataset_type](
        args.data_dir, split="test", **config_util.build_data_options(args))


dset.scene_radius = [dset.scene_radius[0] * args.scene_radius_rescale, dset.scene_radius[1] * args.scene_radius_rescale, dset.scene_radius[2] * args.scene_radius_rescale]

print(f'new scene radius: {dset.scene_radius}')

global_start_time = datetime.now()


ckpt_npz = path.join(args.train_dir, 'ckpt.npz')

if path.isfile(ckpt_npz) and args.load_ckpt:
    print('#####################################################')
    print(f'Resume from ckpt at {ckpt_npz}')
    grid = svox2.SparseGrid.load(ckpt_npz, device=device)
    assert svox2.__dict__['SURFACE_TYPE_' + args.surface_type.upper()] == grid.surface_type, "Loaded ckpt incompatible with given configs"
    gstep_id_base = grid.step_id + 1
    print(f'Starting from step {gstep_id_base}')
    print('#####################################################')
else: 
    grid = svox2.SparseGrid(reso=reso_list[reso_id],
                            center=dset.scene_center,
                            radius=dset.scene_radius,
                            use_sphere_bound=dset.use_sphere_bound and not args.nosphereinit,
                            basis_dim=args.sh_dim,
                            use_z_order=True,
                            device=device,
                            basis_reso=args.basis_reso,
                            basis_type=svox2.__dict__['BASIS_TYPE_' + args.basis_type.upper()],
                            mlp_posenc_size=args.mlp_posenc_size,
                            mlp_width=args.mlp_width,
                            background_nlayers=args.background_nlayers,
                            background_reso=args.background_reso,
                            surface_type=svox2.__dict__['SURFACE_TYPE_' + args.surface_type.upper()],
                            surface_init=args.surface_init,
                            # use_octree=args.renderer_backend != 'surf_trav' or not USE_KERNEL,
                            use_octree=False,
                            trainable_fake_sample_std=args.trainable_fake_sample_std,
                            force_alpha=args.force_alpha)

    # DC -> gray; mind the SH scaling!
    grid.sh_data.data[:] = 0.0
    if args.surface_type != 'none' and args.no_surface_init_iters <= 0:
        grid.density_data.data[:] = -1e8 if args.lr_fg_begin_step > 0 else torch.logit(torch.tensor(args.init_sigma))
    else:
        grid.density_data.data[:] = 0.0 if args.lr_fg_begin_step > 0 else args.init_sigma


    if grid.use_background:
        grid.background_data.data[..., -1] = args.init_sigma_bg
        #  grid.background_data.data[..., :-1] = 0.5 / svox2.utils.SH_C0

    #  grid.sh_data.data[:, 0] = 4.0
    #  osh = grid.density_data.data.shape
    #  den = grid.density_data.data.view(grid.links.shape)
    #  #  den[:] = 0.00
    #  #  den[:, :256, :] = 1e9
    #  #  den[:, :, 0] = 1e9
    #  grid.density_data.data = den.view(osh)

    gstep_id_base = 0

optim_basis_mlp = None

if grid.basis_type == svox2.BASIS_TYPE_3D_TEXTURE and not (path.isfile(ckpt_npz) and args.load_ckpt):
    # do not reinit if resuming from ckpt
    grid.reinit_learned_bases(init_type='sh')
    #  grid.reinit_learned_bases(init_type='fourier')
    #  grid.reinit_learned_bases(init_type='sg', upper_hemi=True)
    #  grid.basis_data.data.normal_(mean=0.28209479177387814, std=0.001)

elif grid.basis_type == svox2.BASIS_TYPE_MLP:
    # MLP!
    optim_basis_mlp = torch.optim.Adam(
                    grid.basis_mlp.parameters(),
                    lr=args.lr_basis
                )


grid.requires_grad_(True)
config_util.setup_render_opts(grid.opt, args)
print('Render options', grid.opt)

if args.no_surface_init_iters > 0:
    # first set step size and skip thresh for cuvol
    grid.opt.sigma_thresh = 1e-8
    grid.opt.step_size = 0.5

resample_cameras = [
        svox2.Camera(c2w.to(device=device),
                     dset.intrins.get('fx', i),
                     dset.intrins.get('fy', i),
                     dset.intrins.get('cx', i),
                     dset.intrins.get('cy', i),
                     width=dset.get_image_size(i)[1],
                     height=dset.get_image_size(i)[0],
                     ndc_coeffs=dset.ndc_coeffs) for i, c2w in enumerate(dset.c2w)
    ]
ckpt_path = path.join(args.train_dir, 'ckpt.npz')

lr_sigma_func = get_expon_lr_func(args.lr_sigma, args.lr_sigma_final, args.lr_sigma_delay_steps,
                                  args.lr_sigma_delay_mult, args.lr_sigma_decay_steps)
lr_alpha_func = get_expon_lr_func(args.lr_alpha, args.lr_alpha_final, args.lr_alpha_delay_steps,
                                  args.lr_alpha_delay_mult, args.lr_alpha_decay_steps, args.lr_alpha_fix_delay)
lr_surface_func = get_expon_lr_func(args.lr_surface, args.lr_surface_final, args.lr_surface_delay_steps,
                                  args.lr_surface_delay_mult, args.lr_surface_decay_steps, args.lr_surf_fix_delay)
lr_fake_sample_std_func = get_expon_lr_func(args.lr_fake_sample_std, args.lr_fake_sample_std_final, args.lr_fake_sample_std_delay_steps,
                                  args.lr_fake_sample_std_delay_mult, args.lr_fake_sample_std_decay_steps)
lr_sh_func = get_expon_lr_func(args.lr_sh, args.lr_sh_final, args.lr_sh_delay_steps,
                               args.lr_sh_delay_mult, args.lr_sh_decay_steps)
lr_sh_surf_func = get_expon_lr_func(args.lr_sh_surf, args.lr_sh_surf_final, args.lr_sh_surf_delay_steps,
                               args.lr_sh_surf_delay_mult, args.lr_sh_surf_decay_steps, args.lr_sh_surf_fix_delay)
lr_basis_func = get_expon_lr_func(args.lr_basis, args.lr_basis_final, args.lr_basis_delay_steps,
                               args.lr_basis_delay_mult, args.lr_basis_decay_steps)
lr_sigma_bg_func = get_expon_lr_func(args.lr_sigma_bg, args.lr_sigma_bg_final, args.lr_sigma_bg_delay_steps,
                               args.lr_sigma_bg_delay_mult, args.lr_sigma_bg_decay_steps)
lr_color_bg_func = get_expon_lr_func(args.lr_color_bg, args.lr_color_bg_final, args.lr_color_bg_delay_steps,
                               args.lr_color_bg_delay_mult, args.lr_color_bg_decay_steps)

fs_std_l1_func = get_linear_lr_func(args.lambda_fake_sample_std_l1, args.lambda_fake_sample_std_l1_final,
                            max_steps=args.lambda_fake_sample_std_l1_steps)

fs_std_l2_func = get_linear_lr_func(args.lambda_fake_sample_std_l2, args.lambda_fake_sample_std_l2_final,
                            max_steps=args.lambda_fake_sample_std_l2_steps)

if args.surf_lv_scale_decay_type == 'linear':
    surf_lv_scale_func = get_linear_lr_func(args.surf_lv_scale, args.surf_lv_scale_final, lr_delay_steps=args.no_surface_init_iters,
                                max_steps=args.surf_lv_scale_decay_steps)
elif args.surf_lv_scale_decay_type == 'exp':
    surf_lv_scale_func = get_expon_lr_func(args.surf_lv_scale, args.surf_lv_scale_final, fix_delay_step=args.no_surface_init_iters,
                                max_steps=args.surf_lv_scale_decay_steps)
else:
    # const
    surf_lv_scale_func = lambda x: args.surf_lv_scale


if args.surf_normal_loss_lambda_type == 'linear':
    lambda_surf_normal_loss_l1_func = get_linear_lr_func(args.lambda_normal_loss_l1, args.lambda_normal_loss_l1_final, 
                                    args.lambda_normal_loss_l1_delay_steps, args.lambda_normal_loss_l1_decay_steps)
    lambda_surf_normal_loss_l2_func = get_linear_lr_func(args.lambda_normal_loss_l2, args.lambda_normal_loss_l2_final, 
                                    args.lambda_normal_loss_l2_delay_steps, args.lambda_normal_loss_l2_decay_steps)



if args.fs_std_decay_type == 'linear':
    fake_sample_std_func = get_linear_lr_func(args.fake_sample_std, args.fake_sample_std_final, lr_delay_steps=args.fake_sample_std_delay,
                                max_steps=args.fake_sample_std_decay_steps)
elif args.fs_std_decay_type == 'exp':
    fake_sample_std_func = get_expon_lr_func(args.fake_sample_std, args.fake_sample_std_final, 0,
                                    1., args.fake_sample_std_decay_steps, args.fake_sample_std_delay)
else:
    # const
    fake_sample_std_func = lambda x: args.fake_sample_std

if args.trunc_vol_a_decay_type == 'linear':
    trunc_vol_a_func = get_linear_lr_func(args.truncated_vol_render_a, args.truncated_vol_render_a_final, lr_delay_steps=args.truncated_vol_render_a_delay,
                                max_steps=args.truncated_vol_render_a_decay_steps)
elif args.trunc_vol_a_decay_type == 'exp':
    trunc_vol_a_func = get_expon_lr_func(args.truncated_vol_render_a, args.truncated_vol_render_a_final, 0,
                                    1., args.truncated_vol_render_a_decay_steps, args.truncated_vol_render_a_delay)
else:
    # const
    trunc_vol_a_func = lambda x: args.truncated_vol_render_a


lr_sigma_factor = 1.0
lr_surface_factor = 1.0
lr_fake_sample_std_factor = 1.0
lr_sh_factor = 1.0
lr_basis_factor = 1.0

last_upsamp_step = args.init_iters

if args.enable_random:
    warn("Randomness is enabled for training (normal for LLFF & scenes with background)")

if args.lambda_tv > 0.0 and args.surface_type in ['udf_alpha']:
    raise NotImplementedError(f'Surface type [{args.surface_type}] must not use density tv!')

surf_lvs_original = None

density_lvs = [ast.literal_eval(args.surf_init_density_lvs[i]) for i in range(len(args.surf_init_density_lvs))]

if args.load_pretrain_density_sh is not None:
    if args.load_pretrain_density_sh.endswith('.npy'):
        # load grid extracted from other nerf
        print(f'Loading density other nerf: {args.load_pretrain_density_sh}')
        density_data = np.load(args.load_pretrain_density_sh)[...,0] * args.density_load_rescale
        density_data = torch.from_numpy(density_data.astype(np.float32)).to(device=device).view(-1,1)

        # perform a quick prunning
        reso = grid.links.shape
        valid_mask = density_data > 0
        valid_mask = valid_mask.view(reso)

        for _ in range(int(2)):
            valid_mask = grid._C.dilate(valid_mask)

        valid_mask = valid_mask.view(-1)
        grid.density_data.data = density_data[valid_mask]
        grid.sh_data.data = grid.sh_data.data[valid_mask]
        grid.surface_data.data = grid.surface_data.data[valid_mask]

        init_links = (
            torch.cumsum(valid_mask.to(torch.int32), dim=-1).int() - 1
        )
        init_links[~valid_mask] = -1

        grid.links = init_links.view(reso).to(device=device)
        kept_ratio = torch.count_nonzero(valid_mask) / valid_mask.numel()
        print(f'{kept_ratio} of the loaded grids has > 0 density')

    else:
        # load from plenoxels
        pretrained_ckpt_path = path.join(args.load_pretrain_density_sh, 'ckpt.npz') if os.path.isdir(args.load_pretrain_density_sh) else \
            args.load_pretrain_density_sh
        
        print(f'Loading density & SH from ckpt: {pretrained_ckpt_path}')

        z = np.load(pretrained_ckpt_path, allow_pickle=True)
        sh_data = z.f.sh_data
        density_data = z.f.density_data * args.density_load_rescale
        links = z.f.links
        sh_data = torch.from_numpy(sh_data.astype(np.float32)).to(device=device)
        density_data = torch.from_numpy(density_data.astype(np.float32)).to(device=device)
        grid.sh_data.data = sh_data
        grid.density_data.data = density_data

        grid.links = torch.from_numpy(links).to(device=device)
        grid.capacity = grid.sh_data.size(0)

        if 'background_data' in z and not args.alphasurf_no_inherit_background_layers:
            background_data = z['background_data']
            background_links = z['background_links']

            background_data = torch.from_numpy(background_data).to(device=device)
            grid.background_nlayers = background_data.shape[1]
            grid.background_reso = background_links.shape[1]
            grid.background_data = torch.nn.Parameter(background_data)
            grid.background_links = torch.from_numpy(background_links).to(device=device)

    grid.accelerate()

    if hasattr(dset.rays, "mask"):
        batch_origins = dset.rays.origins
        batch_dirs = dset.rays.dirs
        batch_mask = dset.rays.mask
        mask_pruning_rays = svox2.Rays(batch_origins, batch_dirs, batch_mask)
    else:
        mask_pruning_rays = None
    # mask_pruning_rays = None

    # grid.opt.near_clip = 0.5

    grid.init_surface_from_density(
        density_lvs=density_lvs,
        reset_alpha=args.surface_init_reset_alpha,
        alpha_rescale=args.surf_init_alpha_rescale,
        surface_rescale=args.surface_init_rescale,
        reset_all=args.surf_init_reset_all,
        prune_threshold=min(density_lvs) / 2,
        init_type=args.surf_init_type,
        weight_init_cams=resample_cameras,
        visibility_pruning_scale=args.visibility_pruning_scale,
        mask_pruning_rays=mask_pruning_rays,
        dilate=args.surf_init_dilate,
        zero_lv_density=args.zero_lv_density,
        )

    # pred_pts = []
    # for lv_set in grid.level_set_data:
    #     pred_pts.append(
    #         grid.extract_pts(n_sample=args.surf_eval_n_sample, density_thresh=args.surf_eval_intersect_th, scene_scale=2./3., to_world=True, surf_lv_set=lv_set)
    #         )
    # pred_pts = torch.concat(pred_pts, axis=0).cpu().detach().numpy()
    # out_dir = f'{args.train_dir}/coarse_pts_eval/{0}'
    # os.makedirs(out_dir, exist_ok=True)
    # np.save(f'{out_dir}/pts.npy', pred_pts)
    # write_vis_pcd(f'{out_dir}/pts.ply', pred_pts)

    # grid.save(f'{args.train_dir}/ckpt_init.npz')

    surf_lvs_original = grid.level_set_data.clone()

    # reset opt for surface rendering
    config_util.setup_render_opts(grid.opt, args)
    print(grid.opt)

epoch_id = -1
while True:
    dset.shuffle_rays()
    epoch_id += 1
    epoch_size = dset.rays.origins.size(0)
    batches_per_epoch = (epoch_size-1)//args.batch_size+1
    # Test
    def eval_step(step_id=gstep_id_base):
        # Put in a function to avoid memory leak
        print('Eval step')
        no_surface = step_id < args.no_surface_init_iters
        with torch.no_grad():
            stats_test = {'psnr' : 0.0, 'mse' : 0.0}

            # Standard set
            # N_IMGS_TO_EVAL = min(20 if epoch_id > 0 else 5, dset_test.n_images)
            N_IMGS_TO_EVAL = args.n_eval_test
            N_IMGS_TO_SAVE = N_IMGS_TO_EVAL # if not args.tune_mode else 1
            img_eval_interval = dset_test.n_images // N_IMGS_TO_EVAL
            img_save_interval = (N_IMGS_TO_EVAL // N_IMGS_TO_SAVE)
            img_ids = range(0, dset_test.n_images, img_eval_interval)

            # img_ids = [120]

            # Special 'very hard' specular + fuzz set
            #  img_ids = [2, 5, 7, 9, 21,
            #             44, 45, 47, 49, 56,
            #             80, 88, 99, 115, 120,
            #             154]
            #  img_save_interval = 1

            n_images_gen = 0
            for i, img_id in tqdm(enumerate(img_ids), total=len(img_ids)):
                c2w = dset_test.c2w[img_id].to(device=device)
                cam = svox2.Camera(c2w,
                                   dset_test.intrins.get('fx', img_id),
                                   dset_test.intrins.get('fy', img_id),
                                   dset_test.intrins.get('cx', img_id),
                                   dset_test.intrins.get('cy', img_id),
                                   width=dset_test.get_image_size(img_id)[1],
                                   height=dset_test.get_image_size(img_id)[0],
                                   ndc_coeffs=dset_test.ndc_coeffs)
                # cam = svox2.Camera(c2w,
                #                    dset_test.intrins.get('fx', img_id) / 8,
                #                    dset_test.intrins.get('fy', img_id) / 8,
                #                    dset_test.intrins.get('cx', img_id) / 8,
                #                    dset_test.intrins.get('cy', img_id) / 8,
                #                    width=100,
                #                    height=100,
                #                    ndc_coeffs=dset_test.ndc_coeffs)
                rgb_pred_test = grid.volume_render_image(cam, use_kernel=USE_KERNEL, no_surface=no_surface)
                rgb_gt_test = dset_test.gt[img_id].to(device=device)
                all_mses = ((rgb_gt_test - rgb_pred_test) ** 2).cpu()
                if i % img_save_interval == 0:
                    img_pred = rgb_pred_test.cpu()
                    img_pred.clamp_max_(1.0)
                    summary_writer.add_image(f'test/image_{img_id:04d}',
                            img_pred, global_step=step_id, dataformats='HWC')
                    # summary_writer.add_image(f'test/gt_image_{img_id:04d}',
                    #         rgb_gt_test, global_step=step_id, dataformats='HWC')
                    if args.log_mse_image:
                        mse_img = all_mses / all_mses.max()
                        summary_writer.add_image(f'test/mse_map_{img_id:04d}',
                                mse_img, global_step=step_id, dataformats='HWC')
                    if args.log_depth_map:
                        depth_img = grid.volume_render_depth_image(cam,
                                    None,
                                    batch_size=10000,
                                    no_surface=no_surface
                                )
                        depth_img = viridis_cmap(depth_img.cpu())
                        summary_writer.add_image(f'test/depth_map_{img_id:04d}',
                                depth_img,
                                global_step=step_id, dataformats='HWC')

                        if args.log_depth_map_use_thresh is not None:
                            depth_thresh_img = grid.volume_render_depth_image(cam,
                                        args.log_depth_map_use_thresh,
                                        batch_size=10000,
                                        no_surface=no_surface
                                        )
                            depth_thresh_img = viridis_cmap(depth_thresh_img.cpu())
                            summary_writer.add_image(f'test/depth_map_thresh_{img_id:04d}',
                                    depth_thresh_img,
                                    global_step=step_id, dataformats='HWC')

                    if args.log_normal_map and not no_surface and grid.opt.backend == 'surf_trav':
                        normal_img = grid.volume_render_normal_image(cam,
                                    # args.log_depth_map_use_thresh,
                                    batch_size=10000
                                )

                        normal_img = normal_img / torch.clamp_min(torch.norm(normal_img, dim=-1, keepdim=True), 1e-8)
                        normal_img = normal_img / 2. + 0.5
                        normal_img = normal_img.cpu()

                        summary_writer.add_image(f'test/normal_map_{img_id:04d}',
                                normal_img,
                                global_step=step_id, dataformats='HWC')

                    if args.log_alpha_map and not no_surface and grid.opt.backend == 'surf_trav':
                        alpha_img = grid.volume_render_alpha_image(cam,
                                    thresh=args.log_depth_map_use_thresh,
                                    # args.log_depth_map_use_thresh,
                                    batch_size=10000
                                )

                        alpha_img = alpha_img.cpu()

                        summary_writer.add_image(f'test/alpha_map_{img_id:04d}',
                                alpha_img,
                                global_step=step_id, dataformats='HWC')

                rgb_pred_test = rgb_gt_test = None
                mse_num : float = all_mses.mean().item()
                psnr = -10.0 * math.log10(mse_num)
                if math.isnan(psnr):
                    print('NAN PSNR', i, img_id, mse_num)
                    assert False 
                stats_test['mse'] += mse_num
                stats_test['psnr'] += psnr
                n_images_gen += 1

            if grid.basis_type == svox2.BASIS_TYPE_3D_TEXTURE or \
               grid.basis_type == svox2.BASIS_TYPE_MLP:
                 # Add spherical map visualization
                EQ_RESO = 256
                eq_dirs = generate_dirs_equirect(EQ_RESO * 2, EQ_RESO)
                eq_dirs = torch.from_numpy(eq_dirs).to(device=device).view(-1, 3)

                if grid.basis_type == svox2.BASIS_TYPE_MLP:
                    sphfuncs = grid._eval_basis_mlp(eq_dirs)
                else:
                    sphfuncs = grid._eval_learned_bases(eq_dirs)
                sphfuncs = sphfuncs.view(EQ_RESO, EQ_RESO*2, -1).permute([2, 0, 1]).cpu().numpy()

                stats = [(sphfunc.min(), sphfunc.mean(), sphfunc.max())
                        for sphfunc in sphfuncs]
                sphfuncs_cmapped = [viridis_cmap(sphfunc) for sphfunc in sphfuncs]
                for im, (minv, meanv, maxv) in zip(sphfuncs_cmapped, stats):
                    cv2.putText(im, f"{minv=:.4f} {meanv=:.4f} {maxv=:.4f}", (10, 20),
                                0, 0.5, [255, 0, 0])
                sphfuncs_cmapped = np.concatenate(sphfuncs_cmapped, axis=0)
                summary_writer.add_image(f'test/spheric',
                        sphfuncs_cmapped, global_step=step_id, dataformats='HWC')
                # END add spherical map visualization

            stats_test['mse'] /= n_images_gen
            stats_test['psnr'] /= n_images_gen


            # log train imgs
            if args.n_eval_train > 0:
                N_IMGS_TO_EVAL = args.n_eval_train
                N_IMGS_TO_SAVE = N_IMGS_TO_EVAL # if not args.tune_mode else 1
                img_eval_interval = dset.n_images // N_IMGS_TO_EVAL
                img_save_interval = (N_IMGS_TO_EVAL // N_IMGS_TO_SAVE)
                img_ids = range(0, dset.n_images, img_eval_interval)
            else:
                img_ids = []
            for i, img_id in tqdm(enumerate(img_ids), total=len(img_ids)):
                c2w = dset.c2w[img_id].to(device=device)
                cam = svox2.Camera(c2w,
                                   dset.intrins.get('fx', img_id),
                                   dset.intrins.get('fy', img_id),
                                   dset.intrins.get('cx', img_id),
                                   dset.intrins.get('cy', img_id),
                                   width=dset.get_image_size(img_id)[1],
                                   height=dset.get_image_size(img_id)[0],
                                   ndc_coeffs=dset.ndc_coeffs)
                rgb_pred_test = grid.volume_render_image(cam, use_kernel=USE_KERNEL, no_surface=no_surface)
                rgb_gt_test = dset.gt[img_id].to(device=device)
                all_mses = ((rgb_gt_test - rgb_pred_test) ** 2).cpu()
                if i % img_save_interval == 0:
                    img_pred = rgb_pred_test.cpu()
                    img_pred.clamp_max_(1.0)
                    summary_writer.add_image(f'train/image_{img_id:04d}',
                            img_pred, global_step=step_id, dataformats='HWC')
                    if args.log_mse_image:
                        mse_img = all_mses / all_mses.max()
                        summary_writer.add_image(f'train/mse_map_{img_id:04d}',
                                mse_img, global_step=step_id, dataformats='HWC')
                    if args.log_depth_map:
                        depth_img = grid.volume_render_depth_image(cam,
                                    None,
                                    batch_size=10000,
                                    no_surface=no_surface
                                )
                        depth_img = viridis_cmap(depth_img.cpu())
                        summary_writer.add_image(f'train/depth_map_{img_id:04d}',
                                depth_img,
                                global_step=step_id, dataformats='HWC')
                        if args.log_depth_map_use_thresh is not None:
                            depth_thresh_img = grid.volume_render_depth_image(cam,
                                        args.log_depth_map_use_thresh,
                                        batch_size=10000,
                                        no_surface=no_surface
                                        )
                            depth_thresh_img = viridis_cmap(depth_thresh_img.cpu())
                            summary_writer.add_image(f'train/depth_map_thresh_{img_id:04d}',
                                    depth_thresh_img,
                                    global_step=step_id, dataformats='HWC')

            if args.log_pts and not no_surface:
                pred_pts = []
                for lv_set in grid.level_set_data:
                    pred_pts.append(
                        grid.extract_pts(n_sample=args.surf_eval_n_sample, density_thresh=args.surf_eval_intersect_th, scene_scale=2./3., to_world=True, surf_lv_set=lv_set)
                        )
                pred_pts = torch.concat(pred_pts, axis=0).cpu().detach().numpy()

                if args.eval_cf:
                    if pred_pts.size > 0:   
                        # downsample points
                        nn_engine = skln.NearestNeighbors(n_neighbors=1, radius=0.001, algorithm='kd_tree', n_jobs=-1)
                        nn_engine.fit(pred_pts)
                        rnn_idxs = nn_engine.radius_neighbors(pred_pts, radius=0.001, return_distance=False)
                        mask = np.ones(pred_pts.shape[0], dtype=np.bool_)
                        for curr, idxs in enumerate(rnn_idxs):
                            if mask[curr]:
                                mask[idxs] = 0
                                mask[curr] = 1
                        pred_pts = pred_pts[mask]

                        # load gt
                        surf_gt = np.load(f'{args.data_dir}/shape.npy')
                        # compute cf
                        dist_d2s, dist_s2d = eval_cf(pred_pts, surf_gt, 0.001)
                        mean_d2s = dist_d2s.mean()
                        mean_s2d = dist_s2d.mean()

                        stats_test['cf_d2s'] = mean_d2s
                        stats_test['cf_s2d'] = mean_s2d
                        stats_test['cf_mean'] = (mean_d2s + mean_s2d) / 2.

                        vis_dist = 0.1
                        max_dist = 20
                        R = np.array([[1,0,0]], dtype=np.float64)
                        G = np.array([[0,1,0]], dtype=np.float64)
                        B = np.array([[0,0,1]], dtype=np.float64)
                        W = np.array([[1,1,1]], dtype=np.float64)
                        data_color = np.tile(B, (pred_pts.shape[0], 1))
                        data_alpha = dist_d2s.clip(max=vis_dist) / vis_dist
                        data_color = R * data_alpha + W * (1-data_alpha)
                        data_color[dist_d2s[:,0] >= max_dist] = G
                        stl_color = np.tile(B, (surf_gt.shape[0], 1))
                        stl_alpha = dist_s2d.clip(max=vis_dist) / vis_dist
                        stl_color= R * stl_alpha + W * (1-stl_alpha)
                        stl_color[dist_s2d[:,0] >= max_dist] = G
                        print(mean_d2s, mean_s2d, (mean_d2s + mean_s2d) / 2.)

                        out_dir = f'{args.train_dir}/coarse_pts_eval/{step_id}'
                        os.makedirs(out_dir, exist_ok=True)
                        write_vis_pcd(f'{out_dir}/vis_d2s.ply', pred_pts, data_color)
                        # write_vis_pcd(f'{out_dir}/vis_s2d.ply', surf_gt, stl_color)
                        

                        with open(f'{out_dir}/cf.txt', 'w') as f:
                            f.write(f'Mean d2s: {mean_d2s}\n')
                            f.write(f'Mean s2d: {mean_s2d}\n')
                            f.write(f'Over all: {(mean_d2s + mean_s2d) / 2.}\n')
                else:
                    out_dir = f'{args.train_dir}/coarse_pts_eval/{step_id}'
                    os.makedirs(out_dir, exist_ok=True)
                    write_vis_pcd(f'{out_dir}/pts.ply', pred_pts)

            for stat_name in stats_test:
                summary_writer.add_scalar('test/' + stat_name,
                        stats_test[stat_name], global_step=step_id)
            # summary_writer.add_scalar('epoch_id', float(epoch_id), global_step=step_id)
            print('eval stats:', stats_test)





    # if epoch_id % max(factor, args.eval_every) == 0 and (epoch_id > 0 or not args.tune_mode):
    # if epoch_id % max(factor, args.eval_every) == 0 and (epoch_id > 0):
    #     # NOTE: we do an eval sanity check, if not in tune_mode
    #     eval_step()
    #     gc.collect()

    def train_step():
        global surf_lvs_original
        print('Train step')
        # pbar = tqdm(range(gstep_id_base, gstep_id_base+epoch_size, args.batch_size), total=batches_per_epoch, initial=gstep_id_base)
        pbar = tqdm(
            enumerate(range(gstep_id_base, gstep_id_base+epoch_size, args.batch_size)), 
            total=args.n_iters, 
            initial=gstep_id_base, 
            # miniters=1000,
            miniters=args.refresh_iter,
            )
        stats = {"mse" : 0.0, "psnr" : 0.0, "invsqr_mse" : 0.0}
        for iter_id, batch_begin in pbar:
            gstep_id = iter_id + gstep_id_base
            
            no_surface = gstep_id < args.no_surface_init_iters
            if args.lr_fg_begin_step > 0 and gstep_id == args.lr_fg_begin_step:
                grid.density_data.data[:] = args.init_sigma
            lr_sigma = lr_sigma_func(gstep_id) * lr_sigma_factor
            lr_alpha = lr_alpha_func(gstep_id)
            lr_surface = lr_surface_func(gstep_id) * lr_surface_factor
            lr_fake_sample_std = lr_fake_sample_std_func(gstep_id) * lr_fake_sample_std_factor
            if grid.surface_data is None or no_surface:
                lr_sh = lr_sh_func(gstep_id) * lr_sh_factor
            else:
                lr_sh = lr_sh_surf_func(gstep_id) * lr_sh_factor
            lr_basis = lr_basis_func(gstep_id - args.lr_basis_begin_step) * lr_basis_factor
            lr_sigma_bg = lr_sigma_bg_func(gstep_id - args.lr_basis_begin_step) * lr_basis_factor
            lr_color_bg = lr_color_bg_func(gstep_id - args.lr_basis_begin_step) * lr_basis_factor
            # lr_sigma_bg = args.lr_sigma_bg
            # lr_color_bg = args.lr_color_bg
            if not args.lr_decay:
                lr_sigma = args.lr_sigma * lr_sigma_factor
                lr_alpha = args.lr_alpha
                lr_surface = args.lr_surface * lr_surface_factor
                lr_sh = args.lr_sh * lr_sh_factor
                lr_basis = args.lr_basis * lr_basis_factor

            if args.surf_normal_loss_lambda_type == 'linear':
                lambda_surf_normal_loss_l1 = lambda_surf_normal_loss_l1_func(gstep_id)
                lambda_surf_normal_loss_l2 = lambda_surf_normal_loss_l2_func(gstep_id)
            else:
                lambda_surf_normal_loss_l1 = args.lambda_normal_loss_l1
                lambda_surf_normal_loss_l2 = args.lambda_normal_loss_l2

            # update fake_sample_std if needed
            if grid.opt.surf_fake_sample and not args.trainable_fake_sample_std:
                # grid.fake_sample_std = torch.tensor(fake_sample_std, 
                # device=grid.fake_sample_std.device, dtype=grid.fake_sample_std.dtype)
                grid.fake_sample_std = fake_sample_std_func(gstep_id)
            grid.truncated_vol_render_a = trunc_vol_a_func(gstep_id)
            if args.trunc_vol_render_lv_remove:
                # remove surface level set that definitely won't be used
                intersect_ids = torch.arange(grid.level_set_data.shape[0])
                rws = grid.trunc_vol_render_rw(intersect_ids)
                new_lv_set = grid.level_set_data[rws > 1e-20]
                if len(new_lv_set) < len(grid.level_set_data):
                    # scale density up to allow lower lv data to be optimized
                    grid.density_data.data[:] += args.trunc_vol_lv_rm_density_add * (len(grid.level_set_data) - len(new_lv_set))
                    grid.level_set_data = new_lv_set

            # update surf lv sets if needed
            if surf_lvs_original is not None and len(grid.level_set_data) > 1 and args.surf_lv_scale_decay_type != 'const':
                if gstep_id >= args.surf_lv_scale_decay_steps:
                    # decay to single lv set
                    grid.level_set_data = torch.tensor([0.], dtype=grid.level_set_data.dtype, device=grid.level_set_data.device)
                else:
                    surf_lv_scale = surf_lv_scale_func(gstep_id)
                    grid.level_set_data = surf_lvs_original * surf_lv_scale 
            

            ############ Density Based Surface Init #################
            if (gstep_id == args.no_surface_init_iters) and (args.no_surface_init_iters > 0):
                # _density_backup = grid.density_data.data.detach().clone()
                eval_step(step_id=gstep_id-1)
                if args.no_surface_init_debug_ckpt:
                    ckpt_path = path.join(args.train_dir, f'ckpt_no_surface.npz')
                    print('Saving for no surface init', ckpt_path)
                    grid.save(ckpt_path, step_id=gstep_id)

                grid.init_surface_from_density(
                    density_lvs=density_lvs,
                    reset_alpha=args.surface_init_reset_alpha,
                    alpha_rescale=args.surf_init_alpha_rescale,
                    surface_rescale=args.surface_init_rescale,
                    reset_all=args.surf_init_reset_all,
                    prune_threshold=min(density_lvs) / 2,
                    )

                surf_lvs_original = grid.surface_data.clone()

                # reset opt for surface rendering
                config_util.setup_render_opts(grid.opt, args)
                print(grid.opt)
                # grid.opt.sigma_thresh = np.log(args.sigma_thresh / (1. - args.sigma_thresh))
                # grid.opt.step_size = args.step_size


                # eval_step(step_id=gstep_id)
                if args.no_surface_init_debug_ckpt:
                    # also save a ckpt
                    ckpt_path = path.join(args.train_dir, f'ckpt_surface_init.npz')
                    print('Saving after surface init', ckpt_path)
                    grid.save(ckpt_path, step_id=gstep_id)

                gc.collect()



                # torch.autograd.set_detect_anomaly(True)

            batch_end = min(batch_begin + args.batch_size, epoch_size)
            batch_origins = dset.rays.origins[batch_begin: batch_end]
            batch_dirs = dset.rays.dirs[batch_begin: batch_end]
            batch_mask = dset.rays.mask[batch_begin: batch_end] if hasattr(dset.rays, "mask") else None
            if batch_origins.shape[0] == 0:
                # empty batch, skip
                continue
            rgb_gt = dset.rays.gt[batch_begin: batch_end]
            rays = svox2.Rays(batch_origins, batch_dirs, batch_mask)

            # with Timing("Fused pass"):
            if not USE_KERNEL and not no_surface:
                if args.surface_type != 'none':
                    out = grid._surface_render_gradcheck_lerp(rays, rgb_gt,
                            beta_loss=args.lambda_beta,
                            sparsity_loss=args.lambda_sparsity,
                            randomize=args.enable_random,
                            alpha_weighted_norm_loss=args.alpha_weighted_norm_loss,
                            no_surface=no_surface)
                else:
                    raise NotImplementedError
            else:
                out = grid.volume_render_fused(rays, rgb_gt,
                        beta_loss=args.lambda_beta,
                        sparsity_loss=args.lambda_sparsity if (grid.surface_data is None or no_surface) else args.lambda_inplace_alpha_sparsify,
                        # fused_surf_norm_reg_scale = lambda_surf_normal_loss if args.fused_surf_norm_reg else 0.0, # fused_surf_norm_reg is no longer supported
                        # fused_surf_norm_reg_con_check = not args.no_surf_norm_con_check,
                        # fused_surf_norm_reg_ignore_empty = args.surf_norm_reg_ignore_empty,
                        lambda_l2 = 1 - args.img_lambda_l1_ratio,
                        lambda_l1 = args.img_lambda_l1_ratio,
                        lambda_l_dist = args.lambda_l_dist,
                        lambda_l_entropy = args.lambda_l_entropy if gstep_id<args.l_entropy_step else 0.,
                        no_norm_weight_l_entropy = args.no_norm_weight_l_entropy,
                        lambda_l_dist_a = args.lambda_l_dist_a,
                        lambda_l_entropy_a = args.lambda_l_entropy_a if gstep_id<args.l_entropy_step else 0.,
                        lambda_l_samp_dist = args.lambda_l_samp_dist,
                        lambda_l_di = args.lambda_l_di,
                        l_di_alpha_thresh = args.l_di_alpha_thresh,
                        surf_sparse_alpha_thresh = args.sparsify_surf_thresh,
                        lambda_inplace_surf_sparse = args.lambda_inplace_surf_sparse,
                        lambda_inwards_norm_loss = args.lambda_inwards_norm_loss,
                        lambda_conv_mode_samp = args.lambda_conv_mode_samp if gstep_id<args.conv_mode_step else 0.,
                        l_dist_max_sample = 64,
                        randomize=args.enable_random,
                        no_surface=no_surface)
            # with Timing("loss_comp"):
            mse = F.mse_loss(rgb_gt, out['rgb'])
            if args.sparsify_only_trained_cells:
                trained_cells_mask = grid.sparse_grad_indexer.clone()
                # trained_cells_mask = grid.sparse_grad_indexer

            # eval_step(step_id=gstep_id)

            if not USE_KERNEL and not no_surface:
                # with Timing("Backward pass"):
                # # normalize surface gradient:
                # mse.backward(retain_graph=True)
                # # grid.surface_data.grad.max() / torch.prod((grid._scaling * grid._grid_size())).cuda()
                # # grid.surface_data.grad = grid.surface_data.grad[:, 0] / (torch.prod(torch.stack(svox2.utils.inv_morton_code_3(torch.arange(grid.surface_data.shape[0]).cuda()),dim=-1),axis=-1)+1)
                # grid.surface_data.grad = grid.surface_data.grad / torch.prod(torch.tensor(grid.links.shape, device=device))
                # loss = 0
                loss = F.mse_loss(out['rgb'], rgb_gt) * (1 - args.img_lambda_l1_ratio) + torch.abs(out['rgb'] - rgb_gt).mean() * args.img_lambda_l1_ratio
                if 'extra_loss' in out:
                    loss += args.lambda_l_dist * out['extra_loss'].get('l_dist', 0.)
                    # loss += args.lambda_no_surf_init_density_lap_loss * out['extra_loss'].get('no_surf_init_density_lap_loss', 0.)
                    # loss += args.lambda_normal_loss * out['extra_loss'].get('normal_loss', 0.)
                
                loss.backward()

                # assert not torch.isnan(grid.surface_data.grad).any()


            # Stats
            mse_num : float = mse.detach().item()
            psnr = -10.0 * math.log10(mse_num)
            stats['mse'] += mse_num
            stats['psnr'] += psnr
            stats['invsqr_mse'] += 1.0 / mse_num ** 2
            
            if 'log_stats' in out:
                for k in out['log_stats'].keys():
                    v = out['log_stats'][k]
                    stats[k] = v + stats[k] if k in stats else v

            if (gstep_id + 1) % args.print_every == 0:
                # Print averaged stats
                pbar.set_description(f'epoch {gstep_id // batches_per_epoch} psnr={psnr:.2f}')
                for stat_name in stats:
                    stat_val = stats[stat_name] / args.print_every
                    summary_writer.add_scalar(stat_name, stat_val, global_step=gstep_id)
                    stats[stat_name] = 0.0
                summary_writer.add_scalar("lr_sh", lr_sh, global_step=gstep_id)
                summary_writer.add_scalar("lr_sigma", lr_sigma, global_step=gstep_id)
                summary_writer.add_scalar("lr_alpha", lr_alpha, global_step=gstep_id)
                summary_writer.add_scalar("lr_surface", lr_surface, global_step=gstep_id)
                summary_writer.add_scalar("lambda_surf_normal_loss_l1", lambda_surf_normal_loss_l1, global_step=gstep_id)
                summary_writer.add_scalar("lambda_surf_normal_loss_l2", lambda_surf_normal_loss_l2, global_step=gstep_id)
                if not args.tune_mode:
                    summary_writer.add_scalar("max_density", grid.density_data.max().cpu().detach().numpy(), global_step=gstep_id)
                    summary_writer.add_scalar("min_density", grid.density_data.min().cpu().detach().numpy(), global_step=gstep_id)
                    if grid.surface_data is not None:
                        summary_writer.add_scalar("max_surface", grid.surface_data.max().cpu().detach().numpy(), global_step=gstep_id)
                        summary_writer.add_scalar("min_surface", grid.surface_data.min().cpu().detach().numpy(), global_step=gstep_id)
                        summary_writer.add_scalar("max_surface_grad", grid.surface_data.grad.max().cpu().detach().numpy(), global_step=gstep_id)
                        summary_writer.add_scalar("min_surface_grad", grid.surface_data.grad.min().cpu().detach().numpy(), global_step=gstep_id)
                        summary_writer.add_scalar("mean_surface_grad", torch.abs(grid.surface_data.grad).mean().cpu().detach().numpy(), global_step=gstep_id)
                if torch.is_tensor(grid.fake_sample_std):
                    summary_writer.add_scalar("fake_sample_std", grid.fake_sample_std.item(), global_step=gstep_id)
                if grid.fake_sample_std is not None:
                    summary_writer.add_scalar("fake_sample_std", grid.fake_sample_std, global_step=gstep_id)
                summary_writer.add_scalar("truncated_vol_render_a", grid.truncated_vol_render_a, global_step=gstep_id)
                if grid.basis_type == svox2.BASIS_TYPE_3D_TEXTURE:
                    summary_writer.add_scalar("lr_basis", lr_basis, global_step=gstep_id)
                if grid.use_background:
                    summary_writer.add_scalar("lr_sigma_bg", lr_sigma_bg, global_step=gstep_id)
                    summary_writer.add_scalar("lr_color_bg", lr_color_bg, global_step=gstep_id)
                
                # if not args.tune_mode:
                #     # log alpha inspect
                #     # world_coords = torch.tensor([[-0.1152,  0.0859,  0.1797]], device='cuda:0')
                #     # coords = grid.world2grid(world_coords)
                #     coords = torch.tensor([[101, 135, 161]], device='cuda', dtype=torch.float)
                #     alpha_inspect = grid._C.sample_grid_raw_alpha(
                #         grid._to_cpp(grid_coords=True),
                #         coords,
                #         -20.
                #     )
                #     summary_writer.add_scalar("alpha_inspect", alpha_inspect, global_step=gstep_id)



                if args.weight_decay_sh < 1.0:
                    grid.sh_data.data *= args.weight_decay_sigma
                if args.weight_decay_sigma < 1.0:
                    grid.density_data.data *= args.weight_decay_sh

            #  # For outputting the % sparsity of the gradient
            #  indexer = grid.sparse_sh_grad_indexer
            #  if indexer is not None:
            #      if indexer.dtype == torch.bool:
            #          nz = torch.count_nonzero(indexer)
            #      else:
            #          nz = indexer.size()
            #      with open(os.path.join(args.train_dir, 'grad_sparsity.txt'), 'a') as sparsity_file:
            #          sparsity_file.write(f"{gstep_id} {nz}\n")

            if args.surf_grad_abs_max is not None:
                # apply gradient clipping on surface gradient from rendering loss
                thresh = np.abs(args.surf_grad_abs_max)
                grid.surface_data.grad = torch.clamp_(grid.surface_data.grad, -thresh, thresh)

                assert torch.abs(grid.surface_data.grad).max() <= thresh

            # Apply TV/Sparsity regularizers
            if (grid.surface_data is None or no_surface):
                # TV on sigma
                if args.lambda_tv > 0.0:
                    grid.inplace_tv_grad(grid.density_data.grad,
                            scaling=args.lambda_tv,
                            sparse_frac=args.tv_sparsity,
                            ndc_coeffs=dset.ndc_coeffs,
                            contiguous=args.tv_contiguous)
            else:
                # loses defined for surface

                # TV on alpha
                if args.lambda_tv_alpha > 0.0:
                    grid.inplace_tv_grad(grid.density_data.grad,
                            scaling=args.lambda_tv_alpha,
                            sparse_frac=args.tv_sparsity,
                            ndc_coeffs=dset.ndc_coeffs,
                            contiguous=args.tv_contiguous)

                if args.lambda_tv_surface > 0.0:
                    #  with Timing("tv_inpl"):
                    grid.inplace_tv_surface_grad(grid.surface_data.grad,
                            scaling=args.lambda_tv_surface,
                            sparse_frac=args.tv_surface_sparsity,
                            ndc_coeffs=dset.ndc_coeffs,
                            contiguous=args.tv_contiguous,
                            ignore_edge= not args.surf_tv_use_edge,
                            edge_value=args.surf_tv_edge_value,
                            alpha_dependency=args.surf_tv_alpha_dependency)

                if lambda_surf_normal_loss_l1 > 0.0 and not args.fused_surf_norm_reg:
                    # with Timing("normal_loss"):
                    norm_loss = grid.inplace_surface_normal_grad(grid.surface_data.grad,
                            scaling=lambda_surf_normal_loss_l1,
                            sparse_frac=args.norm_surface_sparsity,
                            ndc_coeffs=dset.ndc_coeffs,
                            contiguous=args.tv_contiguous,
                            # use_kernel=not args.py_surf_norm_reg,
                            connectivity_check=not args.no_surf_norm_con_check,
                            ignore_empty=args.surf_norm_reg_ignore_empty,
                            use_l1=True,
                            )

                    if (gstep_id + 1) % args.print_every == 0 and norm_loss is not None:
                        summary_writer.add_scalar("surf_norm_loss_l1", norm_loss, global_step=gstep_id)

                if lambda_surf_normal_loss_l2 > 0.0 and not args.fused_surf_norm_reg:
                    # with Timing("normal_loss"):
                    norm_loss = grid.inplace_surface_normal_grad(grid.surface_data.grad,
                            scaling=lambda_surf_normal_loss_l2,
                            sparse_frac=args.norm_surface_sparsity,
                            ndc_coeffs=dset.ndc_coeffs,
                            contiguous=args.tv_contiguous,
                            # use_kernel=not args.py_surf_norm_reg,
                            connectivity_check=not args.no_surf_norm_con_check,
                            ignore_empty=args.surf_norm_reg_ignore_empty,
                            use_l1=False,
                            )

                    if (gstep_id + 1) % args.print_every == 0 and norm_loss is not None:
                        summary_writer.add_scalar("surf_norm_loss_l2", norm_loss, global_step=gstep_id)

                if args.lambda_surface_eikonal > 0.0:
                    # with Timing("normal_loss"):
                    eik_loss = grid.inplace_surface_eikonal_grad(grid.surface_data.grad,
                            scaling=args.lambda_surface_eikonal,
                            sparse_frac=args.norm_surface_sparsity,
                            contiguous=args.tv_contiguous,
                            use_kernel=False,
                            )

                    if (gstep_id + 1) % args.print_every == 0 and eik_loss is not None:
                        summary_writer.add_scalar("eik_loss", eik_loss / args.lambda_surface_eikonal, global_step=gstep_id)


                if args.lambda_norm_match_loss > 0.0:
                    norm_match_loss = grid._surface_norm_match_loss_grad_check(
                                grid._get_rand_cells_non_empty(args.norm_surface_sparsity, contiguous=args.tv_contiguous),
                                scaling=args.lambda_norm_match_loss,
                                )
                    if (gstep_id + 1) % args.print_every == 0:
                        summary_writer.add_scalar("norm_match_loss", norm_match_loss.item() / args.lambda_norm_match_loss, global_step=gstep_id)

                if args.lambda_surf_sign_loss > 0.0:
                    # with Timing("normal_loss"):
                    sign_loss = grid.inplace_surface_sign_change_grad(grid.surface_data.grad,
                            scaling=args.lambda_surf_sign_loss,
                            sparse_frac=1,
                            contiguous=args.tv_contiguous,
                            use_kernel=False,
                            )
                    if (gstep_id + 1) % args.print_every == 0 and sign_loss is not None:
                        summary_writer.add_scalar("sign_loss", sign_loss.item() / args.lambda_surf_sign_loss, global_step=gstep_id)

                # if args.lambda_l_dist > 0.:
                #     py_out = grid._surface_render_gradcheck_lerp(rays, rgb_gt,
                #             beta_loss=args.lambda_beta,
                #             sparsity_loss=args.lambda_sparsity,
                #             randomize=args.enable_random,
                #             alpha_weighted_norm_loss=args.alpha_weighted_norm_loss,
                #             no_surface=no_surface)
                #     loss = args.lambda_l_dist * py_out['extra_loss'].get('l_dist', 0.)
                #     loss.backward()
                #     if (gstep_id + 1) % args.print_every == 0:
                #         summary_writer.add_scalar("l_dist", py_out['extra_loss']['l_dist'].item(), global_step=gstep_id)


                if args.lambda_sparsify_alpha > 0.0 or args.lambda_sparsify_surf > 0.0:
                    # with Timing("normal_loss"):
                    grid.inplace_alpha_surf_sparsify_grad(
                            grid.density_data.grad,
                            grid.surface_data.grad,
                            scaling_alpha = args.lambda_sparsify_alpha if gstep_id > args.delay_sparsify_alpha else 0.,
                            scaling_surf = args.lambda_sparsify_surf if gstep_id > args.delay_sparsify_surf else 0.,
                            sparse_frac = args.alpha_surf_sparsify_sparsity,
                            surf_sparse_decrease = args.sparsify_surf_decrease,
                            surf_sparse_thresh = args.sparsify_surf_thresh,
                            alpha_sparsify_bound = args.alpha_sparsify_bound,
                            surf_sparsify_bound = args.surf_sparsify_bound,
                            only_trained_cells = args.sparsify_only_trained_cells,
                            trained_cells_mask = trained_cells_mask if args.sparsify_only_trained_cells else None,
                            contiguous=args.tv_contiguous,
                            )

                    # assert not torch.isnan(grid.density_data.grad).any()

                if args.lambda_viscosity_loss > 0.0:
                    vis_l, grad_norm = grid._surface_viscosity_loss_grad_check(
                                grid._get_rand_cells_non_empty(args.viscosity_sparsity, contiguous=args.tv_contiguous),
                                scaling=args.lambda_viscosity_loss,
                                eta=args.viscosity_eta
                                )
                    if (gstep_id + 1) % args.print_every == 0:
                        summary_writer.add_scalar("viscosity_loss", vis_l.item(), global_step=gstep_id)
                        summary_writer.add_scalar("surf_grad_norm", grad_norm.item(), global_step=gstep_id)

            # if args.lambda_alpha_lap_loss > 0.0:
            #     grid.inplace_alpha_lap_grad(grid.density_data.grad,
            #             scaling=args.lambda_alpha_lap_loss,
            #             sparse_frac=args.alpha_lap_sparsity,
            #             ndc_coeffs=dset.ndc_coeffs,
            #             contiguous=args.tv_contiguous,
            #             # use_kernel=USE_KERNEL,
            #             density_is_sigma = grid.surface_data is None or no_surface 
            #             )

            if args.lambda_tv_sh > 0.0:
                #  with Timing("tv_color_inpl"):
                grid.inplace_tv_color_grad(grid.sh_data.grad,
                        scaling=args.lambda_tv_sh,
                        sparse_frac=args.tv_sh_sparsity,
                        ndc_coeffs=dset.ndc_coeffs,
                        contiguous=args.tv_contiguous)
            if args.lambda_tv_lumisphere > 0.0:
                grid.inplace_tv_lumisphere_grad(grid.sh_data.grad,
                        scaling=args.lambda_tv_lumisphere,
                        dir_factor=args.tv_lumisphere_dir_factor,
                        sparse_frac=args.tv_lumisphere_sparsity,
                        ndc_coeffs=dset.ndc_coeffs)
            if args.lambda_l2_sh > 0.0:
                grid.inplace_l2_color_grad(grid.sh_data.grad,
                        scaling=args.lambda_l2_sh)
            if grid.use_background and (args.lambda_tv_background_sigma > 0.0 or args.lambda_tv_background_color > 0.0):
                grid.inplace_tv_background_grad(grid.background_data.grad,
                        scaling=args.lambda_tv_background_color,
                        scaling_density=args.lambda_tv_background_sigma,
                        sparse_frac=args.tv_background_sparsity,
                        contiguous=args.tv_contiguous)
            if args.lambda_tv_basis > 0.0:
                tv_basis = grid.tv_basis()
                loss_tv_basis = tv_basis * args.lambda_tv_basis
                loss_tv_basis.backward()
            #  print('nz density', torch.count_nonzero(grid.sparse_grad_indexer).item(),
            #        ' sh', torch.count_nonzero(grid.sparse_sh_grad_indexer).item())


            # Manual SGD/rmsprop step
            # with Timing('Optimize step'):
            if gstep_id >= args.lr_fg_begin_step:
                if grid.surface_data is None or no_surface:
                    # optimizing sigma
                    grid.optim_density_step(lr_sigma, beta=args.rms_beta, optim=args.sigma_optim)
                else:
                    # optimizing alpha
                    grid.optim_density_step(lr_alpha, beta=args.rms_beta, optim=args.alpha_optim)
                    
                if not no_surface:
                    if gstep_id < args.surface_init_freeze + args.no_surface_init_iters:
                        grid.surface_data.grad[:] = 0.
                    else:
                        # if args.surf_grad_abs_max is not None:
                        #     # apply gradient clipping
                        #     thresh = np.abs(args.surf_grad_abs_max)
                        #     torch.clamp_(grid.surface_data.grad, -thresh, thresh)
                        grid.optim_surface_step(lr_surface, beta=args.rms_beta, optim=args.surface_optim)

                    if args.trainable_fake_sample_std:
                        grid.optim_fake_sample_std_step(lr_fake_sample_std, beta=args.rms_beta, optim=args.surface_optim, 
                        lambda_l1=fs_std_l1_func(gstep_id),
                        lambda_l2=fs_std_l2_func(gstep_id),
                        )
                        with torch.no_grad():
                            torch.clamp_min_(grid.fake_sample_std, 1e-8)

                grid.optim_sh_step(lr_sh, beta=args.rms_beta, optim=args.sh_optim)
            if grid.use_background:
                grid.optim_background_step(lr_sigma_bg, lr_color_bg, beta=args.rms_beta, optim=args.bg_optim)
            if gstep_id >= args.lr_basis_begin_step:
                if grid.basis_type == svox2.BASIS_TYPE_3D_TEXTURE:
                    grid.optim_basis_step(lr_basis, beta=args.rms_beta, optim=args.basis_optim)
                elif grid.basis_type == svox2.BASIS_TYPE_MLP:
                    optim_basis_mlp.step()
                    optim_basis_mlp.zero_grad()

            if ((gstep_id % args.eval_every_iter) == 0) or (gstep_id == args.surface_init_freeze + args.no_surface_init_iters and args.surface_init_freeze > 0): # and gstep_id > 0:
                if not no_surface:
                    eval_step(step_id=gstep_id)
                    gc.collect()

            if (gstep_id % args.extract_mesh_every == 0) and gstep_id > 0 and not no_surface and not args.tune_mode:
                obj_path = path.join(args.train_dir, 'mesh', f'mesh_{gstep_id:05d}.obj')
                os.makedirs(path.join(args.train_dir, 'mesh'), exist_ok=True)
                grid.extract_mesh(obj_path, args.mesh_sigma_thresh)

            # if (gstep_id % args.extract_pts_every == 0) and not args.tune_mode:
            #     ply_path = path.join(args.train_dir, 'pts_coarse', f'pts_{gstep_id:05d}.ply')
            #     os.makedirs(path.join(args.train_dir, 'pts_coarse'), exist_ok=True)
            #     pts = grid.extract_pts(n_sample=args.surf_eval_n_sample, density_thresh=args.surf_eval_intersect_th, scene_scale=2./3., to_world=True)

            if gstep_id >= args.n_iters:
                print('* Final eval and save')
                global_stop_time = datetime.now()
                secs = (global_stop_time - global_start_time).total_seconds()
                timings_file = open(os.path.join(args.train_dir, 'time_mins.txt'), 'a')
                timings_file.write(f"{secs / 60}\n")
                # if args.trunc_vol_render_lv_end_remove:
                #     # remove surface level set that definitely won't be used
                #     intersect_ids = torch.arange(grid.level_set_data.shape[0])
                #     rws = grid.trunc_vol_render_rw(intersect_ids)
                #     grid.level_set_data = grid.level_set_data[rws > 1e-8]
                if not args.tune_nosave:
                    ckpt_path = path.join(args.train_dir, f'ckpt.npz')
                    grid.save(ckpt_path, step_id=gstep_id)
                
                if args.final_render_eval:
                    with torch.no_grad():
                        stats_test = {'psnr' : 0.0, 'mse' : 0.0}

                        n_images_gen = 0
                        for img_id in tqdm(range(dset_test.c2w.shape[0]), total=dset_test.c2w.shape[0]):
                            c2w = dset_test.c2w[img_id].to(device=device)
                            cam = svox2.Camera(c2w,
                                            dset_test.intrins.get('fx', img_id),
                                            dset_test.intrins.get('fy', img_id),
                                            dset_test.intrins.get('cx', img_id),
                                            dset_test.intrins.get('cy', img_id),
                                            width=dset_test.get_image_size(img_id)[1],
                                            height=dset_test.get_image_size(img_id)[0],
                                            ndc_coeffs=dset_test.ndc_coeffs)

                            rgb_pred_test = grid.volume_render_image(cam, use_kernel=USE_KERNEL, no_surface=no_surface)
                            rgb_gt_test = dset_test.gt[img_id].to(device=device)
                            all_mses = ((rgb_gt_test - rgb_pred_test) ** 2).cpu()

                            rgb_pred_test = rgb_gt_test = None
                            mse_num : float = all_mses.mean().item()
                            psnr = -10.0 * math.log10(mse_num)
                            if math.isnan(psnr):
                                assert False 
                            stats_test['mse'] += mse_num
                            stats_test['psnr'] += psnr
                            n_images_gen += 1
                        
                        mean_psnr = stats_test['psnr'] / n_images_gen
                        with (Path(args.train_dir) / 'render_eval.txt').open('w') as f:
                            f.write(f'PSNR: {mean_psnr}')
                        summary_writer.add_scalar('final_eval/psnr', mean_psnr, global_step=gstep_id)

                exit(0)
            
            if args.save_every > 0 and gstep_id % args.save_every == 0 and not args.tune_mode and gstep_id > 0:
                if not no_surface:
                    if args.save_all_ckpt:
                        ckpt_path = path.join(args.train_dir, f'ckpt_{gstep_id:05d}.npz')
                    else:
                        ckpt_path = path.join(args.train_dir, f'ckpt.npz')
                    print('Saving', ckpt_path)
                    grid.save(ckpt_path, step_id=gstep_id)


            global last_upsamp_step, reso_id, reso_list, factor
            if (gstep_id - last_upsamp_step) >= args.upsamp_every:
                last_upsamp_step = gstep_id
                if reso_id < len(reso_list) - 1:
                    print('* Upsampling from', reso_list[reso_id], 'to', reso_list[reso_id + 1])
                    if args.tv_early_only > 0:
                        print('turning off TV regularization')
                        args.lambda_tv = 0.0
                        args.lambda_tv_alpha = 0.0
                        args.lambda_tv_sh = 0.0
                    elif args.tv_decay != 1.0:
                        args.lambda_tv *= args.tv_decay
                        args.lambda_tv_alpha *= args.tv_decay
                        args.lambda_tv_sh *= args.tv_decay

                    # ckpt_path = path.join(args.train_dir, f'ckpt_{grid.links.shape[0]}_last.npz')
                    # print('Saving', ckpt_path)
                    # grid.save(ckpt_path, step_id=gstep_id)

                    # eval_step(step_id=gstep_id)
                    # gc.collect()

                    reso_id += 1
                    use_sparsify = True
                    z_reso = reso_list[reso_id] if isinstance(reso_list[reso_id], int) else reso_list[reso_id][2]

                    if grid.surface_data is None or no_surface:
                        grid.resample(reso=reso_list[reso_id],
                                sigma_thresh=args.density_thresh,
                                weight_thresh=args.weight_thresh / z_reso if use_sparsify else 0.0,
                                dilate=2, #use_sparsify,
                                cameras=resample_cameras if args.thresh_type == 'weight' else None,
                                max_elements=args.max_grid_elements)
                    else:
                        grid_ratio = grid.resample_surface(reso=reso_list[reso_id],
                                alpha_thresh=args.alpha_upsample_thresh,
                                weight_thresh=args.weight_thresh / z_reso if use_sparsify else 0.0,
                                dilate=2, #use_sparsify,
                                cameras=resample_cameras if args.thresh_type == 'weight' else None,
                                max_elements=args.max_grid_elements,
                                non_expanding=args.surf_non_expand)

                        summary_writer.add_scalar("grid_ratio", grid_ratio, global_step=gstep_id)

                    if grid.use_background and reso_id <= 1:
                        grid.sparsify_background(args.background_density_thresh)

                    if args.upsample_density_add:
                        grid.density_data.data[:] += args.upsample_density_add
                    
                    # ckpt_path = path.join(args.train_dir, f'ckpt_{grid.links.shape[0]}_begin.npz')
                    # print('Saving', ckpt_path)
                    # grid.save(ckpt_path, step_id=gstep_id)

                    # eval_step(step_id=gstep_id+1)
                    # gc.collect()

                if factor > 1 and reso_id < len(reso_list) - 1:
                    print('* Using higher resolution images due to large grid; new factor', factor)
                    factor //= 2
                    dset.gen_rays(factor=factor)
                    dset.shuffle_rays()

    train_step()
    gc.collect()
    gstep_id_base += batches_per_epoch

    #  ckpt_path = path.join(args.train_dir, f'ckpt_{epoch_id:05d}.npz')
    # Overwrite prev checkpoints since they are very huge
    # if args.save_every > 0 and (epoch_id + 1) % max(
    #         factor, args.save_every) == 0 and not args.tune_mode:
    #     print('Saving', ckpt_path)
    #     grid.save(ckpt_path)


    # if gstep_id_base >= args.n_iters:
    #     print('* Final eval and save')
    #     # eval_step()
    #     global_stop_time = datetime.now()
    #     secs = (global_stop_time - global_start_time).total_seconds()
    #     timings_file = open(os.path.join(args.train_dir, 'time_mins.txt'), 'a')
    #     timings_file.write(f"{secs / 60}\n")
    #     if not args.tune_nosave:
    #         grid.save(ckpt_path)
    #     break

#
# Authors: Bowen Wen
# Contact: wenbowenxjtu@gmail.com
# Created in 2020
#
# Copyright (c) Rutgers University, 2020 All rights reserved.
#
# Wen, B., C. Mitash, B. Ren, and K. E. Bekris. "se (3)-TrackNet:
# Data-driven 6D Pose Tracking by Calibrating Image Residuals in
# Synthetic Domains." In IEEE/RSJ International Conference on Intelligent
# Robots and Systems (IROS). 2020.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
#     * Redistributions of source code must retain the above copyright
#       notice, this list of conditions and the following disclaimer.
#     * Redistributions in binary form must reproduce the above copyright
#       notice, this list of conditions and the following disclaimer in the
#       documentation and/or other materials provided with the distribution.
#     * Neither the name of the PRACSYS, Bowen Wen, Rutgers University,
#       nor the names of its contributors may be used to
#       endorse or promote products derived from this software without
#       specific prior written permission.
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS 'AS IS' AND
# ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED
# WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
# DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER BE LIABLE FOR ANY
# DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES
# (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES;
# LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND
# ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
# (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS
# SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
#

import open3d as o3d
import sys,math,random
import os,subprocess,pdb
import re
import scipy.io
dir_path = os.path.dirname(os.path.realpath(__file__))
sys.path.append(dir_path)
sys.path.append(dir_path+'/scripts/')
from eval_ycb import VOCap
from multiprocessing import cpu_count
import argparse
import torch
from torch import optim
from Utils import *
import numpy as np
import yaml
from data_augmentation import *
from se3_tracknet import *
from datasets import *
from offscreen_renderer import *
import matplotlib.pyplot as plt
from torch.utils.data import DataLoader
import numpy as np
import cv2
from PIL import Image
import copy
import glob
import mpl_toolkits.mplot3d.axes3d as p3
import transformations as T
import Utils as U
from scipy import spatial
from multiprocessing import Pool
import multiprocessing
from functools import partial
from itertools import repeat
import multiprocessing as mp
from vispy_renderer import VispyRenderer
import queue
from concurrent.futures import Future
import time
import gc


random.seed(0)
np.random.seed(0)
torch.manual_seed(0)
torch.backends.cudnn.benchmark = True


def project_points(points,K):
  us = np.divide(points[:,0]*K[0,0],points[:,2]) + K[0,2]
  vs = np.divide(points[:,1]*K[1,1],points[:,2]) + K[1,2]
  us = np.round(us).astype(np.int32).reshape(-1,1)
  vs = np.round(vs).astype(np.int32).reshape(-1,1)
  return np.hstack((us,vs))


def use_posecnn_res(class_id,seq_frame_str):
  with open('{}/image_sets/keyframe.txt'.format(ycb_dir),'r') as ff:
    lines = ff.readlines()
  seq_frames = []
  for i in range(len(lines)):
    seq_frames.append(lines[i].rstrip())

  seq_id = int(seq_frame_str.split('/')[0])
  start_frame = int(seq_frame_str.split('/')[1])
  neighbor = 0
  while 1:
    tmp = '%04d/%06d'%(seq_id,start_frame+neighbor)
    if tmp in seq_frames:
      start_frame = start_frame+neighbor
      index = seq_frames.index(tmp)
      break
    tmp= '%04d/%06d'%(seq_id,start_frame-neighbor)
    if tmp in seq_frames:
      start_frame = start_frame-neighbor
      index = seq_frames.index(tmp)
      break
    neighbor += 1

  print('Using posecnn at ',tmp)
  index = seq_frames.index(tmp)
  posecnn_res = scipy.io.loadmat('/YCB_Video_toolbox/results_PoseCNN_RSS2018/%06d.mat'.format(args.ycb_dir)%(index))
  id = np.where(posecnn_res['rois'][:,1]==class_id)
  tmp = posecnn_res['poses_icp'][id].reshape(-1)
  quat = tmp[:4]
  R = T.quaternion_matrix(quat)[:3,:3]
  xyz = tmp[4:]
  pose = np.eye(4)
  pose[:3,:3] = R
  pose[:3,3] = xyz
  return pose

def print_gpu_memory():
    print(f'Allocated: {torch.cuda.memory_allocated() / 1024 ** 2} MB')
    print(f'Cached: {torch.cuda.memory_reserved() / 1024 ** 2} MB')
    print(torch.cuda.memory_summary(device=torch.cuda.current_device()))


class Tracker:
  def __init__(self, dataset_info, images_mean, images_std, ckpt_dir, model_path=None, trans_normalizer=0.03, rot_normalizer=5*np.pi/180):
    self.vispy_renderer_queue = queue.Queue()
    self.dataset_info = dataset_info
    self.image_size = (dataset_info['resolution'], dataset_info['resolution'])
    self.model_path = model_path
    mesh = trimesh.load(self.model_path)
    self.object_cloud = toOpen3dCloud(mesh.vertices)
    self.object_cloud = self.object_cloud.voxel_down_sample(voxel_size=0.005)
    
    print('self.object_cloud loaded and downsampled')
    if 'object_width' not in dataset_info:
      object_max_width = compute_obj_max_width(np.asarray(self.object_cloud.points))
      bounding_box = dataset_info['boundingbox']
      with_add = bounding_box / 100 * object_max_width
      self.object_width = object_max_width + with_add
    else:
      self.object_width = dataset_info['object_width']
    print('self.object_width=',self.object_width)

    self.mean = images_mean
    self.std = images_std
    cam_cfg = dataset_info['camera']
    self.K = np.array([cam_cfg['focalX'], 0, cam_cfg['centerX'], 0, cam_cfg['focalY'], cam_cfg['centerY'], 0,0,1]).reshape(3,3)

    print('Loading ckpt from ',ckpt_dir)
    checkpoint = torch.load(ckpt_dir)
    #checkpoint['state_dict'] = checkpoint['state_dict'].cpu()
    checkpoint_state_dict = {k: v.cpu() for k, v in checkpoint['state_dict'].items()}

    print("vorher_1:")
    print_gpu_memory()
    del checkpoint
    gc.collect()
    torch.cuda.empty_cache()
    print("nachher_1:")
    print_gpu_memory()
  
    #if 'epoch' in checkpoint:
    #  print('pose track ckpt epoch={}'.format(checkpoint['epoch']))

    self.model = Se3TrackNet(image_size=self.image_size[0])
    self.model.load_state_dict(checkpoint_state_dict)
    self.model = self.model.cuda()
    #self.model.cpu()
    self.model.eval()
    
    #print(checkpoint['state_dict']['convA1.0.weight'])
    

    #if 'renderer' in dataset_info and dataset_info['renderer']=='pyrenderer':
    #  print('Using pyrenderer')
    #  assert '.obj' in model_path
    #  self.renderer = Renderer([model_path],self.K,cam_cfg['height'],cam_cfg['width'])
    #else:
    #  print('Using vispy renderer')
    #  # if '.obj' in model_path:
    #  #   mesh = trimesh.load(model_path)
    #  #   img = np.asarray(mesh.visual.material.image)[...,:3]
    #  #   H,W = img.shape[:2]
    #  #   uv = mesh.visual.uv * np.array([W-1, H-1]).reshape(1,2)
    #  #   uv = uv.round().astype(int)
    #  #   vc = img[::-1][uv[:,1], uv[:,0]]
    #  #   visual_new = trimesh.visual.ColorVisuals(mesh=mesh, vertex_colors=vc)
    #  #   mesh.visual = visual_new
    #  #   new_model_path = model_path.replace('.obj','.ply')
    #  #   mesh.export(new_model_path)
    #  #   # pdb.set_trace()
    #  #   model_path = new_model_path
    #  # pdb.set_trace()
    #  assert '.ply' in model_path
    #  self.renderer = VispyRenderer(model_path, self.K, H=dataset_info['resolution'], W=dataset_info['resolution'])

    if '.obj' in self.model_path:
      print('Using pyrenderer')
      self.renderer = Renderer([self.model_path],self.K,cam_cfg['height'],cam_cfg['width'])
    else:
      print('Using vispy renderer')
      assert '.ply' in self.model_path
      self.renderer = VispyRenderer(self.model_path, self.K, H=dataset_info['resolution'], W=dataset_info['resolution'])

    self.prev_rgb = None
    self.prev_depth = None
    self.frame_cnt = 0
    self.errs = []

    posttransforms = Compose([OffsetDepth(),NormalizeChannels(images_mean, images_std),ToTensor()])

    self.dataset = TrackDataset('','eval',images_mean, images_std,None,None,posttransforms,dataset_info, trans_normalizer=trans_normalizer, rot_normalizer=rot_normalizer)

  def render_window(self, ob2cam):
    '''
    @ob2cam: 4x4 mat ob in opencv cam
    '''

    glcam_in_cvcam = np.array([[1,0,0,0],
                              [0,-1,0,0],
                              [0,0,-1,0],
                              [0,0,0,1]])
    
    bbox = compute_bbox(ob2cam, self.K, self.object_width, scale=(1000,-1000,1000))#, scale=(1000, -1000, 1000))
    
    ob2cam_gl = np.linalg.inv(glcam_in_cvcam).dot(ob2cam)
    left = np.min(bbox[:, 1])  # 1
    right = np.max(bbox[:, 1]) # 1
    top = np.min(bbox[:, 0])   # 0
    bottom = np.max(bbox[:, 0])# 0
    if isinstance(self.renderer,VispyRenderer):
      #print("Vispy render")

      s1 = time.time_ns()
      self.renderer.update_cam_mat(self.K, left, right, bottom, top)
      #self.renderer.update_cam_mat(self.K, left, right, bottom, top)
      #self.renderer.update_cam_mat(self.K,0,self.K[0,2]*2,self.K[1,2]*2,0)# muss später wieder auskommentiert werden
      render_rgb, render_depth = self.renderer.render_image(ob2cam_gl)
      #render_rgb, render_depth = self.renderer.render_image(ob2cam_gl)
      
      #render_rgb, render_depth = self.renderer.render_image(ob2cam)
      
    else:
      #print("No Vispy Render")
      bbox = compute_bbox(ob2cam, self.K, self.object_width,scale=(1000,1000,1000))#, scale=(1000, 1000, 1000))
      rgb, depth = self.renderer.render([ob2cam])
      depth = (depth*1000).astype(np.uint16)
      render_rgb, render_depth = crop_bbox(rgb, depth, bbox, self.image_size)
    return render_rgb, render_depth
  # ermittelt ob das Objekt innerhalb der Kamera liegt
  def is_object_window(self,pose):
    bb = compute_bbox(pose, self.K, self.object_width,scale=(1000,1000,1000))
    img_shape = (int(self.K[0,2]*2),int(self.K[1,2]*2))
    width = img_shape[0]
    height  = img_shape[1]
    mid = bb[0] + ((bb[3]-bb[0])/2)
    #print(bb)
    #print((bb[3]-bb[0])/2)
    #print(mid)
    if mid[1] < 0 or mid[1] > width or mid[0] < 0 or mid[0] > height:
      return False
    return True
  def on_track(self, prev_pose, current_rgb, current_depth, gt_A_in_cam=None,gt_B_in_cam=None, debug=False, samples=1):
    
    if not self.is_object_window(prev_pose):
        print("OBJEKT IS NICHT AUF FOTO")
        return prev_pose
    #end test section
    #print(f"TIME A: {round(time.time() * 1000)}")
    K = self.K
    A_in_cam = prev_pose.copy()
    bbs = []
    sample_poses = []
    rgbBs = []
    depthBs = []
    for i in range(samples):
      if i==0:
        sample_pose = prev_pose.copy()
      
      bb = compute_bbox(sample_pose, self.K, self.object_width,scale=(1000,1000,1000))#, scale=(1000, 1000, 1000))
      bbs.append(bb)
      #print(bb)

      # print bb
      cv2.rectangle(current_rgb, (bb[0,1],bb[0,0]), (bb[3,1],bb[3,0]), (0, 255, 0), 2)
      cv2.imshow("Bounding Box", current_rgb)

      sample_poses.append(sample_pose)
      rgbB, depthB = crop_bbox(current_rgb, current_depth, bb, self.image_size)
      rgbBs.append(rgbB)
      depthBs.append(depthB)
    sample_poses = np.array(sample_poses)
    bbs = np.array(bbs)

    rgbAs = []
    depthAs = []
    maskAs = []
    for i in range(samples):
      rgbA, depthA = self.render_window(sample_poses[i])
      maskA = depthA>100
      rgbAs.append(rgbA)
      depthAs.append(depthA)
      maskAs.append(maskA)
    rgbAs,depthAs,maskAs = list(map(np.array, [rgbAs,depthAs,maskAs]))
    rgbAs_backup = rgbAs.copy()
    rgbBs_backup = rgbBs.copy()

    if gt_B_in_cam is None:
      #print('**** gt_B_in_cam set to Identity')
      gt_B_in_cam = np.eye(4)
    #print(f"TIME B: {round(time.time() * 1000)}")
      
    dataAs = []
    dataBs = []
    for i in range(samples):
      sample = self.dataset.processData(rgbAs[i],depthAs[i],sample_poses[i],rgbBs[i],depthBs[i],gt_B_in_cam)[0]
      dataAs.append(sample[0].unsqueeze(0))
      dataBs.append(sample[1].unsqueeze(0))
    #dataA = torch.cat(dataAs,dim=0).cpu().float() 
    dataA = torch.cat(dataAs,dim=0).cuda().float()
    #dataB = torch.cat(dataBs,dim=0).cpu().float() 
    dataB = torch.cat(dataBs,dim=0).cuda().float()
    #print(f"TIME C: {round(time.time() * 1000)}")
    s1 = time.time_ns()
    with torch.no_grad():
      prediction = self.model(dataA,dataB)
    #print(f"TIME D: {round(time.time() * 1000)}")

    pred_B_in_cams = []
    for i in range(samples):
      trans_pred = prediction['trans'][i].data.cpu().numpy()
      rot_pred = prediction['rot'][i].data.cpu().numpy()
      pred_B_in_cam = self.dataset.processPredict(sample_poses[i],(trans_pred,rot_pred))
      pred_B_in_cams.append(pred_B_in_cam)

    pred_B_in_cams = np.array(pred_B_in_cams)
    #pred_B_in_cams = np.array([A_in_cam]) # muss später wieder auskommentiert werden
    final_estimate = pred_B_in_cams[0].copy()
    self.prev_rgb = current_rgb
    self.prev_depth = current_depth
    pred_color, pred_depth = self.render_window(final_estimate)
    #pred_color, pred_depth = self.render_window(np.array(A_in_cam)) # muss später wieder auskommentiert werden

    #test section
    #pred_color2, pred_depth = self.render_window(final_estimate)
    #pred_color1, pred_depth = self.render_window(np.array(A_in_cam))
    #canv = makeCanvas([pred_color1, pred_color2], flipBR=False)
    #cv2.imshow("TEST",canv)
    
    
    canvas = makeCanvas([rgbBs_backup[0],rgbAs_backup[0], pred_color], flipBR=False)
    #print(f"TIME E: {round(time.time() * 1000)}")
    cv2.imshow('AB',canvas)
    cv2.waitKey(10)
    #print(f"TIME F: {round(time.time() * 1000)}")
    self.frame_cnt += 1

    if samples==1:
      return pred_B_in_cams[0]
    
    return pred_B_in_cams[0]

  def update_cam(self,cam_K):
    self.K = cam_K.copy()
    img_shape = (int(self.K[0,2]*2),int(self.K[1,2]*2))
    if '.obj' in self.model_path:
      print('Updating pyrenderer')
      self.renderer = Renderer([self.model_path],self.K,img_shape[1],img_shape[0])
      print(f"height:{img_shape[1]}, width: {img_shape[0]}, K: {self.K}")
    else:
      print('Updating vispy renderer')
      assert '.ply' in self.model_path
      self.renderer = VispyRenderer(self.model_path, self.K, H=self.dataset_info['resolution'], W=self.dataset_info['resolution'])

def getResultsYcb():
  debug = False
  initialize_method = 'gt'   #choose from ---- gt, posecnn, poserbpf
  reinit_frames = None
  if args.reinit_frames is not None:
    reinit_frames = args.reinit_frames.split(',')
  class_id = args.class_id
  samples = 1
  out_dir = outdir

  if not os.path.exists(out_dir):
    os.makedirs(out_dir)
  preds = []
  gts = []
  seq_ids = []
  frame_ids = []
  test_data_dir = '{}/data_organized/'.format(args.ycb_dir)
  gt_dirs = glob.glob(test_data_dir+'**/pose_gt')
  gt_dirs.sort()
  tracker = Tracker(dataset_info, images_mean, images_std,ckpt_dir, model_path=model_path)
  keyframe_begin_id_map = {}
  with open('{}/image_sets/keyframe.txt'.format(args.ycb_dir),'r') as ff:
    lines = ff.readlines()
  for i in range(len(lines)):
    lines[i] = lines[i].rstrip()
    tmp = lines[i].split('/')
    seq = int(tmp[0])
    frame_id = int(tmp[1])
    if seq not in keyframe_begin_id_map.keys():
      keyframe_begin_id_map[seq] = frame_id   #In keyframe.txt, frame id starts from 1
  keyframes_all = lines

  seqs = U.findClassContainedVideosYcb(class_id,testset=True)
  seqs.sort()
  print('Found seqs: ',seqs)
  tmp = []
  for gt_dir in gt_dirs:
    seq_id = int(re.findall(r'/\d{4}/',gt_dir)[0][1:-1])
    if seq_id not in seqs:
      continue
    tmp.append(gt_dir)
  gt_dirs = np.array(tmp)

  tested_video = 0
  for gt_dir in gt_dirs:
    gt_dir = gt_dir+'/'
    class_indices = list(map(int,os.listdir(gt_dir)))
    if class_id not in class_indices:
      continue
    seq_id = int(re.findall(r'/\d{4}/',gt_dir)[0][1:-1])
    if seq_id<48 or seq_id>59:  #Skip non-test set
      continue
    rgb_files = glob.glob(gt_dir+'../color/*.png')
    rgb_files.sort()
    depth_files = glob.glob(gt_dir+'../depth_filled/*.png')
    depth_files.sort()
    gt_files = glob.glob(gt_dir+'{}/*.txt'.format(class_id))
    gt_files.sort()
    seg_files = glob.glob(gt_dir+'../seg/*.png')
    seg_files.sort()

    seq_frame = '%04d/%06d'%(seq_id,1)

    if initialize_method=='posecnn':   #Load PoseCNN estimate as initialization
      posecnn_res_dir = '{}/YCB_Video_toolbox/results_PoseCNN_RSS2018/'.format(args.ycb_dir)
      print('Loading PoseCNN estimate from ',posecnn_res_dir+'%06d.mat'%(keyframes_all.index(seq_frame)))
      posecnn_res = scipy.io.loadmat(posecnn_res_dir+'%06d.mat'%(keyframes_all.index(seq_frame)))
      id = np.where(posecnn_res['rois'][:,1]==class_id)
      tmp = posecnn_res['poses_icp'][id].reshape(-1)
      quat = tmp[:4]
      R = T.quaternion_matrix(quat)[:3,:3]
      xyz = tmp[4:]
      prev_pose = np.eye(4)
      prev_pose[:3,:3] = R
      prev_pose[:3,3] = xyz
    elif initialize_method=='poserbpf':
      res_dir = '{}/YCB_Video_toolbox/PoseRBPF_Results/YCB_results_RGBD/'.format(args.ycb_dir)
      folders = sorted(os.listdir(res_dir))
      cur_res_dir = res_dir+folders[class_id-1]+'/'
      cur_res_dir = cur_res_dir+'seq_{}/'.format(seqs.index(seq_id)+1)
      print('poserbpf cur_res_dir\n',cur_res_dir)
      file_dir = glob.glob(cur_res_dir+'Pose*.txt')[0]
      with open(file_dir,'r') as ff:
        line = ff.readlines()[0].rstrip()
      pose = line.split()[2:]
      tmp = np.eye(4)
      tmp[:3,3] = pose[:3]
      q_wxyz = pose[3:]
      tmp[:3,:3] = T.quaternion_matrix(q_wxyz)[:3,:3]
      prev_pose = tmp.copy()
    elif initialize_method=='gt':
      prev_pose = np.loadtxt(gt_files[0])

    mat = {}
    mat['poses'] = prev_pose
    mat['frame_id'] = 1
    mat['seq_id'] = seq_id
    mat['gt_pose'] = np.loadtxt(gt_files[0])
    print('init pose\n',prev_pose)
    print('gt_pose\n',np.loadtxt(gt_files[0]))

    tmp = cv2.imread(rgb_files[0])
    H = tmp.shape[0]
    W = tmp.shape[1]
    writer = cv2.VideoWriter(out_dir+'seq{}.mp4'.format(seq_id),cv2.VideoWriter_fourcc(*'mp4v'), fps=30, frameSize=(W//2,H//2))
    K = tracker.K.copy()
    pred_poses = [prev_pose]
    for i in range(1,len(rgb_files)):
      if i%100==0:
        print('>>>>>>>>>>>>>>> {}, {} / {}'.format(seq_id, i, len(rgb_files)))
      frame_id = i+1
      rgb = np.array(Image.open(rgb_files[i]))
      rgb_viz = rgb.copy()
      depth = cv2.imread(depth_files[i], cv2.IMREAD_UNCHANGED).astype(np.uint16)
      gt_B_in_cam = np.loadtxt(gt_files[i])

      # try:
      cur_pose = tracker.on_track(prev_pose, rgb, depth, gt_A_in_cam=None,gt_B_in_cam=gt_B_in_cam, debug=debug,samples=samples)
      # except Exception as e:
      # 	print("ERROR ",e)
      # 	cur_pose = prev_pose.copy()
      prev_pose = cur_pose.copy()
      pred_poses.append(cur_pose)
      seq_frame = '%04d/%06d'%(seq_id,frame_id)

      model = copy.deepcopy(tracker.object_cloud)
      model.transform(cur_pose)
      uvs = project_points(np.asarray(model.points).copy(),K)
      cur_bgr = cv2.cvtColor(rgb_viz,cv2.COLOR_RGB2BGR)
      cv2.putText(cur_bgr,"frame:{}".format(frame_id), (W//2,H-50), cv2.FONT_HERSHEY_SIMPLEX, fontScale=1,thickness=4,color=(255,0,0))

      for ii in range(len(uvs)):
        cv2.circle(cur_bgr,(uvs[ii,0],uvs[ii,1]),radius=1,color=(0,255,255),thickness=-1)
      cur_bgr = cv2.resize(cur_bgr,(W//2,H//2))
      writer.write(cur_bgr)

    writer.release()
    tested_video += 1
    if len(pred_poses)!=len(rgb_files):
      last_pose = pred_poses[-1]
      for _ in range(len(rgb_files)-len(pred_poses)):
        pred_poses.append(last_pose)
    os.makedirs(out_dir+'seq{}'.format(seq_id),exist_ok=True)
    for i in range(len(pred_poses)):
      np.savetxt(out_dir+'seq{}/%07d.txt'.format(seq_id)%(i),pred_poses[i])


def predictSequenceYcb():
  init = 'gt'
  seq_id = args.seq_id
  debug = False
  test_data_path = '{}/%04d'.format(args.ycb_dir)%(seq_id)
  class_id = 4
  if args.class_id is not None:
    class_id = args.class_id
  start_frame = 0
  reinit_frames = ''
  if args.reinit_frames is not None:
    reinit_frames = args.reinit_frames.split(',')
    print('reinit_frames',reinit_frames)
  samples = 1
  rgb_files = glob.glob(os.path.join(test_data_path,'color/*'))
  rgb_files.sort()
  depth_files = glob.glob(os.path.join(test_data_path,'depth_filled/*'))
  depth_files.sort()
  gt_poses = []
  gt_pose_files = glob.glob(os.path.join(test_data_path,'pose_gt/{}/*'.format(class_id)))
  gt_pose_files.sort()
  seg_files = glob.glob(os.path.join(test_data_path,'seg/*'))
  seg_files.sort()
  for f in gt_pose_files:
    gt = np.loadtxt(f)
    gt_poses.append(gt)
    # break

  tracker = Tracker(dataset_info, images_mean, images_std,ckpt_dir, model_path=model_path)
  print('gt_poses[0]=\n',gt_poses[start_frame])

  if init=='gt':
    prev_pose = gt_poses[start_frame].copy()
  elif init=='posecnn':
    with open('{}/image_sets/keyframe.txt'.format(args.ycb_dir),'r') as ff:
      lines = ff.readlines()
    seq_frames = []
    for i in range(len(lines)):
      seq_frames.append(lines[i].rstrip())
    neighbor = 0
    while 1:
      seq_frame_str= '%04d/%06d'%(seq_id,start_frame+neighbor)
      if seq_frame_str in seq_frames:
        start_frame = start_frame+neighbor
        index = seq_frames.index(seq_frame_str)
        break
      seq_frame_str= '%04d/%06d'%(seq_id,start_frame-neighbor)
      if seq_frame_str in seq_frames:
        start_frame = start_frame-neighbor
        index = seq_frames.index(seq_frame_str)
        break
      neighbor += 1
    prev_pose = use_posecnn_res(class_id,seq_frame_str)
  elif init=='poserbpf':
    seqs = U.findClassContainedVideosYcb(class_id,testset=True)
    seqs.sort()
    res_dir = '{}/YCB_Video_toolbox/PoseRBPF_Results/YCB_results_RGBD/'.format(args.ycb_dir)
    folders = sorted(os.listdir(res_dir))
    cur_res_dir = res_dir+folders[class_id-1]+'/'
    cur_res_dir = cur_res_dir+'seq_{}/'.format(seqs.index(seq_id)+1)
    print('poserbpf cur_res_dir\n',cur_res_dir)
    file_dir = glob.glob(cur_res_dir+'Pose*.txt')[0]
    with open(file_dir,'r') as ff:
      line = ff.readlines()[0].rstrip()
    pose = line.split()[2:]
    tmp = np.eye(4)
    tmp[:3,3] = pose[:3]
    q_wxyz = pose[3:]
    tmp[:3,:3] = T.quaternion_matrix(q_wxyz)[:3,:3]
    prev_pose = tmp.copy()


  pred_poses = [prev_pose]
  prev_second_pose = None

  K = tracker.K.copy()

  out_dir = outdir
  os.makedirs(out_dir,exist_ok=True)
  tmp = cv2.imread(depth_files[0],cv2.IMREAD_UNCHANGED)
  H = tmp.shape[0]
  W = tmp.shape[1]

  for i in range(start_frame+1,len(rgb_files)):
    if i%100==0:
      print('>>>>>>>>>>>>>>>>',i)
    rgb = np.array(Image.open(rgb_files[i]))
    rgb_viz = rgb.copy()
    depth = cv2.imread(depth_files[i], cv2.IMREAD_UNCHANGED).astype(np.uint16)

    A_in_cam = prev_pose.copy()

    seq_frame_str = '%04d/%06d'%(seq_id,i+1)
    if seq_frame_str in reinit_frames:
      A_in_cam = use_posecnn_res(class_id,'%04d/%06d'%(seq_id,i-1))
      print('Reinitialized at ',i)

    cur_pose = tracker.on_track(A_in_cam, rgb, depth, gt_A_in_cam=gt_poses[i-1],gt_B_in_cam=gt_poses[i], debug=debug,samples=samples)
    A_in_cam = cur_pose.copy()

    prev_pose = cur_pose.copy()
    pred_poses.append(cur_pose)

    model = copy.deepcopy(tracker.object_cloud)
    model.transform(cur_pose)
    K = tracker.K.copy()
    uvs = project_points(np.asarray(model.points),K)
    cur_bgr = cv2.cvtColor(rgb_viz,cv2.COLOR_RGB2BGR)
    for ii in range(len(uvs)):
      cv2.circle(cur_bgr,(uvs[ii,0],uvs[ii,1]),radius=1,color=(0,255,255),thickness=-1)
    cv2.putText(cur_bgr,"frame:{}".format(i), (W//2,H-50), cv2.FONT_HERSHEY_SIMPLEX, fontScale=1,thickness=4,color=(255,0,0))
    cv2.imshow('1',cur_bgr)
    if debug:
      cv2.imwrite(out_dir+'%07d.png'%(i),cur_bgr)
    cur_bgr = cv2.resize(cur_bgr,(W//2,H//2))
    if i==1:
      cv2.waitKey(0)
    else:
      cv2.waitKey(1)
  pred_poses = np.array(pred_poses)

  adi_errs = []
  for i in range(len(pred_poses)):
    np.savetxt(out_dir+'%05d.txt'%(i), pred_poses[i])
    np.savetxt(out_dir+'%05dgt.txt'%(i),gt_poses[i])
    err = U.adi(pred_poses[i],gt_poses[i],tracker.object_cloud)
    adi_errs.append(err)

  adi_auc = VOCap(np.array(adi_errs))*100
  print('reinit_frames {}, adi_auc {}'.format(reinit_frames,adi_auc))


def predictSequenceYcbInEOAT():
  samples = 1
  debug = False
  test_data_path = args.YCBInEOAT_dir
  print(test_data_path)
  rgb_files = sorted(glob.glob('{}/rgb/*.png'.format(test_data_path)))
  depth_files = sorted(glob.glob('{}/depth_filled/*.png'.format(test_data_path)))

  tracker = Tracker(dataset_info, images_mean, images_std,ckpt_dir, model_path=model_path, trans_normalizer=0.03, rot_normalizer=30*np.pi/180)

  gt_files = sorted(glob.glob('{}/annotated_poses/*.txt'.format(test_data_path)))
  gt_poses = []
  for i in range(len(gt_files)):
    gt_pose = np.loadtxt(gt_files[i])
    gt_poses.append(gt_pose)
  prev_pose = gt_poses[0].copy()
  print('init pose\n',prev_pose)
  K = tracker.K.copy()

  out_dir = outdir
  os.makedirs(out_dir,exist_ok=True)
  tmp = cv2.imread(depth_files[0],cv2.IMREAD_UNCHANGED)
  H = tmp.shape[0]
  W = tmp.shape[1]

  for i in range(len(rgb_files)):
    rgb = np.array(Image.open(rgb_files[i]))[:,:,:3]
    rgb_viz = rgb.copy()
    depth = cv2.imread(depth_files[i], cv2.IMREAD_UNCHANGED).astype(np.uint16)

    A_in_cam = prev_pose.copy()
    cur_pose = tracker.on_track(A_in_cam, rgb, depth, gt_A_in_cam=np.eye(4),gt_B_in_cam=np.eye(4), debug=debug,samples=samples)
    prev_pose = cur_pose.copy()
    np.savetxt(out_dir+'%07d.txt'%(i),cur_pose)
    model = copy.deepcopy(tracker.object_cloud)
    model.transform(cur_pose)
    uvs = project_points(np.asarray(model.points),K)
    cur_bgr = cv2.cvtColor(rgb_viz,cv2.COLOR_RGB2BGR)
    for ii in range(len(uvs)):
      cv2.circle(cur_bgr,(uvs[ii,0],uvs[ii,1]),radius=1,color=(0,255,255),thickness=-1)
    cv2.putText(cur_bgr,"frame:{}".format(i), (W//2,H-50), cv2.FONT_HERSHEY_SIMPLEX, fontScale=1,thickness=4,color=(255,0,0))
    if i==0:
      cv2.waitKey(0)
    else:
      cv2.waitKey(1)
    cur_bgr = cv2.resize(cur_bgr,(W//2,H//2))


if __name__ == '__main__':
  parser = argparse.ArgumentParser()
  parser.add_argument('--mode', default='ycbv', help='ycbv / ycbineoat ')
  parser.add_argument('--seq_id', default=None, type=int)
  parser.add_argument('--ycb_dir', default='/media/bowen/e25c9489-2f57-42dd-b076-021c59369fec/DATASET/Tracking/YCB_Video_Dataset')
  parser.add_argument('--YCBInEOAT_dir', default='/media/bowen/e25c9489-2f57-42dd-b076-021c59369fec/catkin_ws/src/iros20_dataset/video_rosbag/IROS_SELECTED/FINISHED_LABEL.iros_submission_version/bleach0')
  parser.add_argument('--train_data_path', help="train_data_path path", default="None", type=str)
  parser.add_argument('--class_id', default=-1, type=int, help='class id in YCB Video')
  parser.add_argument('--model_path', type=str, help='path to mesh')
  parser.add_argument('--ckpt_dir', type=str)
  parser.add_argument('--mean_std_path', type=str)
  parser.add_argument('--outdir', help="save res dir", type=str, default='/home/bowen/debug/')
  parser.add_argument('--reinit_frames', type=str, default=None,help='reinit to compare with PoseRBPF')

  args = parser.parse_args()

  ckpt_dir = args.ckpt_dir
  mean_std_path = args.mean_std_path
  model_path = args.model_path

  print('ckpt_dir:',ckpt_dir)

  train_data_path = args.train_data_path
  outdir = args.outdir

  dataset_info_path = os.path.join(train_data_path,'../dataset_info.yml')
  print('dataset_info_path',dataset_info_path)
  with open(dataset_info_path,'r') as ff:
    dataset_info = yaml.safe_load(ff)

  images_mean = np.load(os.path.join(mean_std_path, "mean.npy"))
  images_std = np.load(os.path.join(mean_std_path, "std.npy"))

  if args.mode=='ycbv':
    predictSequenceYcb()
  elif args.mode=='ycbineoat':
    predictSequenceYcbInEOAT()
  else:
    getResultsYcb()



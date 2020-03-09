import glob
import os

import cv2
import numpy as np
import torch
from torchvision import transforms

from gaze_estimation_models_pytorch import GazeEstimationmodelResnet18
from rt_gene.estimate_gaze_pytorch import GazeEstimator
from rt_gene.extract_landmarks_method_base import LandmarkMethodBase
from rt_gene.gaze_tools import get_phi_theta_from_euler, limit_yaw

__script_path = os.path.dirname(os.path.realpath(__file__))

_landmark_estimator = LandmarkMethodBase(device_id_facedetection="cuda:0",
                                         checkpoint_path_face=os.path.abspath(
                                             os.path.join(__script_path, "../../rt_gene/model_nets/SFD/s3fd_facedetector.pth")),
                                         checkpoint_path_landmark=os.path.abspath(
                                             os.path.join(__script_path, "../../rt_gene/model_nets/phase1_wpdc_vdc.pth.tar")),
                                         model_points_file=os.path.abspath(os.path.join(__script_path, "../../rt_gene/model_nets/face_model_68.txt")))

_transform = transforms.Compose([lambda x: cv2.resize(x, dsize=(224, 224), interpolation=cv2.INTER_CUBIC),
                                 transforms.ToTensor(),
                                 transforms.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225])])

device_id_gazeestimation = "cuda:0"


def load_model(ckpts):
    models = []
    for ckpt in ckpts:
        _model = GazeEstimationmodelResnet18(num_out=2)
        _torch_load = torch.load(os.path.join(__script_path, "../../rt_gene/model_nets/{}".format(ckpt)))['state_dict']
        _state_dict = {k[7:]: v for k, v in _torch_load.items()}
        _model.load_state_dict(_state_dict)
        _model.to(device_id_gazeestimation)
        _model.eval()
        models.append(_model)
    return models


def extract_eye_image_patches(subject):
    le_c, re_c, le_bb, re_bb = subject.get_eye_image_from_landmarks(subject.transformed_eye_landmarks, subject.face_color,
                                                                    _landmark_estimator.eye_image_size)
    subject.left_eye_color = le_c
    subject.right_eye_color = re_c
    subject.left_eye_bb = le_bb
    subject.right_eye_bb = re_bb


_img_list = glob.glob(os.path.join(__script_path, "../../rt_gene/samples/*.jpg"))
models = load_model(["rt_gene_pytorch_checkpoints/_ckpt_epoch_1.ckpt"])

try:
    # _cap = cv2.VideoCapture(0)
    # while True:
    for file in _img_list:
        frame = cv2.imread(file)
        ret = True
        # ret, frame = _cap.read()
        if ret:
            im_width, im_height = frame.shape[1], frame.shape[0]
            _dist_coefficients, _camera_matrix = np.zeros((1, 5)), np.array(
                [[im_height, 0.0, im_width / 2.0], [0.0, im_height, im_height / 2.0], [0.0, 0.0, 1.0]])

            faceboxes = _landmark_estimator.get_face_bb(frame)

            if len(faceboxes) > 0:
                subjects = _landmark_estimator.get_subjects_from_faceboxes(frame, faceboxes)
                subject = subjects[0]
                extract_eye_image_patches(subject)

                if subject.left_eye_color is None or subject.right_eye_color is None:
                    continue

                success, rotation_vector, _ = cv2.solvePnP(_landmark_estimator.model_points,
                                                           subject.landmarks.reshape(len(subject.landmarks), 1, 2),
                                                           cameraMatrix=_camera_matrix,
                                                           distCoeffs=_dist_coefficients,
                                                           flags=cv2.SOLVEPNP_DLS)

                if not success:
                    continue

                roll_pitch_yaw = [-rotation_vector[2], -rotation_vector[0], rotation_vector[1] + np.pi]
                roll_pitch_yaw = limit_yaw(np.array(roll_pitch_yaw).flatten().tolist())

                head_pose = get_phi_theta_from_euler(roll_pitch_yaw)

                _left = subject.left_eye_color.astype('uint8')
                _right = subject.right_eye_color.astype('uint8')

                _transformed_left = _transform(_left).to(device_id_gazeestimation).unsqueeze(0)
                _transformed_right = _transform(_right).to(device_id_gazeestimation).unsqueeze(0)
                _head_pose = torch.from_numpy(np.array([*head_pose])).to(device_id_gazeestimation).unsqueeze(0).float()

                gaze = [model(_transformed_left, _transformed_right, _head_pose).detach().cpu() for model in models]
                gaze1 = torch.stack(gaze, dim=1)
                gaze2 = torch.mean(gaze1, dim=1).numpy()

                l_gaze_img = GazeEstimator.visualize_eye_result(subject.left_eye_color, gaze2.tolist()[0])
                r_gaze_img = GazeEstimator.visualize_eye_result(subject.right_eye_color, gaze2.tolist()[0])
                s_gaze_img = np.concatenate((r_gaze_img, l_gaze_img), axis=1)

                cv2.imshow("face", frame)
                cv2.imshow("patches", s_gaze_img)
                cv2.waitKey(0)

except KeyboardInterrupt:
    cv2.destroyAllWindows()
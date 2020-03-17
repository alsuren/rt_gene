import os
from argparse import ArgumentParser
from functools import partial

import h5py
import numpy as np
import pytorch_lightning as pl
import torch
from PIL import Image, ImageFilter
from pytorch_lightning.callbacks import ModelCheckpoint
from torch.utils.data import DataLoader
from torchvision.transforms import transforms

from rt_gene.gaze_estimation_models_pytorch import GazeEstimationModelMobileNetV2, GazeEstimationModelResnet18, GazeEstimationModelResnet50, \
    GazeEstimationModelVGG
from rtgene_dataset import RTGENEH5Dataset
from utils.GazeAngleAccuracy import GazeAngleAccuracy
from utils.PinballLoss import PinballLoss


class TrainRTGENE(pl.LightningModule):

    def __init__(self, hparams, train_subjects, validate_subjects, test_subjects):
        super(TrainRTGENE, self).__init__()
        _loss_fn = {
            "mse": partial(torch.nn.MSELoss, reduction="sum"),
            "pinball": partial(PinballLoss, reduction="sum")
        }
        _param_num = {
            "mse": 2,
            "pinball": 3
        }
        _models = {
            "vgg": partial(GazeEstimationModelVGG, num_out=_param_num.get(hparams.loss_fn)),
            "mobilenet": partial(GazeEstimationModelMobileNetV2, num_out=_param_num.get(hparams.loss_fn)),
            "resnet18": partial(GazeEstimationModelResnet18, num_out=_param_num.get(hparams.loss_fn)),
            "resnet50": partial(GazeEstimationModelResnet50, num_out=_param_num.get(hparams.loss_fn))
        }
        self._model = _models.get(hparams.model_base)()
        self._criterion = _loss_fn.get(hparams.loss_fn)()
        self._angle_acc = GazeAngleAccuracy()
        self._train_subjects = train_subjects
        self._validate_subjects = validate_subjects
        self._test_subjects = test_subjects
        self.hparams = hparams

    def forward(self, left_patch, right_patch, head_pose):
        return self._model(left_patch, right_patch, head_pose)

    def training_step(self, batch, batch_idx):
        _left_patch, _right_patch, _headpose_label, _gaze_labels = batch

        angular_out = self.forward(_left_patch, _right_patch, _headpose_label)
        loss = self._criterion(angular_out, _gaze_labels)
        angle_acc = self._angle_acc(angular_out[:, :2], _gaze_labels)
        tensorboard_logs = {'train_loss': loss, 'train_angle': angle_acc}
        return {'loss': loss, 'log': tensorboard_logs}

    def validation_step(self, batch, batch_idx):
        _left_patch, _right_patch, _headpose_label, _gaze_labels = batch

        angular_out = self.forward(_left_patch, _right_patch, _headpose_label)
        loss = self._criterion(angular_out, _gaze_labels)
        angle_acc = self._angle_acc(angular_out[:, :2], _gaze_labels)

        return {'val_loss': loss, "angle_acc": angle_acc}

    def validation_end(self, outputs):
        _losses = torch.stack([x['val_loss'] for x in outputs])
        _angles = np.array([x['angle_acc'] for x in outputs])
        tensorboard_logs = {'val_loss': _losses.mean(), 'val_angle': np.mean(_angles)}
        return {'val_loss': _losses.mean(), 'log': tensorboard_logs}

    def test_step(self, batch, batch_idx):
        _left_patch, _right_patch, _headpose_label, _gaze_labels = batch

        angular_out = self.forward(_left_patch, _right_patch, _headpose_label)
        angle_acc = self._angle_acc(angular_out[:, :2], _gaze_labels)

        return {"angle_acc": angle_acc}

    def test_end(self, outputs):
        _angles = np.array([x['angle_acc'] for x in outputs])
        _mean = np.mean(_angles)
        _std = np.std(_angles)
        return {'test_angle_mean': _mean, 'test_angle_std': _std}

    def configure_optimizers(self):
        _params_to_update = []
        for name, param in self._model.named_parameters():
            if param.requires_grad:
                _params_to_update.append(param)

        _learning_rate = self.hparams.learning_rate
        _optimizer = torch.optim.Adam(_params_to_update, lr=_learning_rate, betas=(0.9, 0.95))
        return _optimizer

    @staticmethod
    def add_model_specific_args(parent_parser, root_dir):
        parser = ArgumentParser(parents=[parent_parser])
        parser.add_argument('--augment', action="store_true", dest="augment")
        parser.add_argument('--no_augment', action="store_false", dest="augment")
        parser.add_argument('--loss_fn', choices=["mse", "pinball"], default="mse")
        parser.add_argument('--batch_size', default=128, type=int)
        parser.add_argument('--learning_rate', type=float, default=0.000325)
        parser.add_argument('--model_base', choices=["vgg", "mobilenet", "resnet18", "resnet50"], default="vgg")
        return parser

    def train_dataloader(self):
        _train_transforms = None
        if self.hparams.augment:
            _train_transforms = transforms.Compose([transforms.RandomResizedCrop(size=(224, 224), scale=(0.85, 1.0)),
                                                    transforms.RandomRotation(degrees=5),
                                                    transforms.Resize((224, 224), Image.BICUBIC),
                                                    transforms.RandomGrayscale(p=0.08),
                                                    lambda x: x if np.random.random_sample() > 0.08 else x.filter(ImageFilter.GaussianBlur(radius=5)),
                                                    lambda x: x if np.random.random_sample() > 0.08 else x.filter(ImageFilter.GaussianBlur(radius=8)),
                                                    transforms.ToTensor(),
                                                    transforms.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225])])
        _data_train = RTGENEH5Dataset(h5_file=h5py.File(self.hparams.hdf5_file, mode="r"),
                                      subject_list=self._train_subjects,
                                      transform=_train_transforms)
        return DataLoader(_data_train, batch_size=self.hparams.batch_size, shuffle=True, num_workers=self.hparams.num_io_workers, pin_memory=False)

    def val_dataloader(self):
        _data_validate = RTGENEH5Dataset(h5_file=h5py.File(self.hparams.hdf5_file, mode="r"), subject_list=self._validate_subjects)
        return DataLoader(_data_validate, batch_size=self.hparams.batch_size, shuffle=True, num_workers=self.hparams.num_io_workers, pin_memory=False)

    def test_dataloader(self):
        _data_test = RTGENEH5Dataset(h5_file=h5py.File(self.hparams.hdf5_file, mode="r"), subject_list=self._test_subjects)
        return DataLoader(_data_test, batch_size=self.hparams.batch_size, shuffle=True, num_workers=self.hparams.num_io_workers, pin_memory=False)


if __name__ == "__main__":
    from pytorch_lightning import Trainer

    root_dir = os.path.dirname(os.path.realpath(__file__))

    _root_parser = ArgumentParser(add_help=False)
    _root_parser.add_argument('--gpu', type=int, default=1, help='gpu to use, can be repeated for mutiple gpus i.e. --gpu 1 --gpu 2', action="append")
    _root_parser.add_argument('--hdf5_file', type=str, default=os.path.abspath(os.path.join(root_dir, "../../RT_GENE/dataset.hdf5")))
    _root_parser.add_argument('--save_dir', type=str, default=os.path.abspath(os.path.join(root_dir, '../../rt_gene/model_nets/pytorch_checkpoints')))
    _root_parser.add_argument('--benchmark', action='store_true', dest="benchmark")
    _root_parser.add_argument('--no-benchmark', action='store_false', dest="benchmark")
    _root_parser.add_argument('--num_io_workers', default=8, type=int)
    _root_parser.add_argument('--k_fold_validation', default=False, type=bool)
    _root_parser.set_defaults(benchmark=True)
    _root_parser.set_defaults(augment=False)

    _model_parser = TrainRTGENE.add_model_specific_args(_root_parser, root_dir)
    _hyperparams = _model_parser.parse_args()

    if _hyperparams.benchmark:
        torch.backends.cudnn.benchmark = True

    _train_subjects = []
    _valid_subjects = []
    _test_subjects = []
    if _hyperparams.k_fold_validation is False:
        _train_subjects.append([0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15])
        _valid_subjects.append([16])
    else:
        # this is provided by scikit-learn KFold class, but presented here to avoid adding further dependencies
        _train_subjects.append([0, 1, 2, 8, 10, 3, 4, 7, 9])
        _train_subjects.append([0, 1, 2, 8, 10, 5, 6, 11, 12, 13])
        _train_subjects.append([3, 4, 7, 9, 5, 6, 11, 12, 13])
        # validation set is always subjects 14, 15 and 16
        _valid_subjects.append([14, 15, 16])
        _valid_subjects.append([14, 15, 16])
        _valid_subjects.append([14, 15, 16])
        # test subjects
        _test_subjects.append([5, 6, 11, 12, 13])
        _test_subjects.append([3, 4, 7, 9])
        _test_subjects.append([0, 1, 2, 8, 10])

    for fold, (train_s, valid_s, test_s) in enumerate(zip(_train_subjects, _valid_subjects, _test_subjects)):
        complete_path = os.path.abspath(os.path.join(_hyperparams.save_dir, "fold_{}/".format(fold)))

        _model = TrainRTGENE(hparams=_hyperparams, train_subjects=train_s, validate_subjects=valid_s, test_subjects=test_s)
        # save all models
        checkpoint_callback = ModelCheckpoint(filepath=complete_path, monitor='val_loss', mode='min', verbose=False, save_top_k=-1)
        # start training
        trainer = Trainer(gpus=_hyperparams.gpu,
                          checkpoint_callback=checkpoint_callback,
                          progress_bar_refresh_rate=1,
                          max_epochs=5)
        trainer.fit(_model)
        trainer.test()
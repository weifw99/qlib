# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.


from __future__ import division
from __future__ import print_function

import os

import numpy as np
import pandas as pd
from typing import Text, Union
import copy
from ...utils import get_or_create_path
from ...log import get_module_logger

import torch
import torch.nn as nn
import torch.optim as optim
from torch.utils.data import DataLoader

from .pytorch_utils import count_parameters
from ...model.base import Model
from ...data.dataset import DatasetH
from ...data.dataset.handler import DataHandlerLP
from ...model.utils import ConcatDataset
from ...data.dataset.weight import Reweighter


class ALSTM(Model):
    """ALSTM Model

    Parameters
    ----------
    d_feat : int
        input dimension for each time step
    metric: str
        the evaluation metric used in early stop
    optimizer : str
        optimizer name
    GPU : int
        the GPU ID used for training
    """

    def __init__(
        self,
        d_feat=6,
        hidden_size=64,
        num_layers=2,
        dropout=0.0,
        n_epochs=200,
        lr=0.001,
        metric="",
        batch_size=2000,
        early_stop=20,
        loss="mse",
        optimizer="adam",
        n_jobs=10,
        GPU=0,
        seed=None,
        init_model_path=None,
        **kwargs,
    ):
        # Set logger.
        self.logger = get_module_logger("ALSTM")
        self.logger.info("ALSTM pytorch version...")

        # set hyper-parameters.
        self.d_feat = d_feat
        self.hidden_size = hidden_size
        self.num_layers = num_layers
        self.dropout = dropout
        self.n_epochs = n_epochs
        self.lr = lr
        self.metric = metric
        self.batch_size = batch_size
        self.early_stop = early_stop
        self.optimizer = optimizer.lower()
        self.loss = loss
        self.device = torch.device("cuda:%d" % (GPU) if torch.cuda.is_available() and GPU >= 0 else "mps" if torch.backends.mps.is_available() else "cpu")
        self.n_jobs = n_jobs
        self.seed = seed

        self.logger.info(
            "ALSTM parameters setting:"
            "\nd_feat : {}"
            "\nhidden_size : {}"
            "\nnum_layers : {}"
            "\ndropout : {}"
            "\nn_epochs : {}"
            "\nlr : {}"
            "\nmetric : {}"
            "\nbatch_size : {}"
            "\nearly_stop : {}"
            "\noptimizer : {}"
            "\nloss_type : {}"
            "\ndevice : {}"
            "\nn_jobs : {}"
            "\nuse_GPU : {}"
            "\nseed : {}"
            "\ninit_model_path: {}"
            "\nkwargs: {}".format(
                d_feat,
                hidden_size,
                num_layers,
                dropout,
                n_epochs,
                lr,
                metric,
                batch_size,
                early_stop,
                optimizer.lower(),
                loss,
                self.device,
                n_jobs,
                self.use_gpu,
                seed,
                init_model_path,
                kwargs,
            )
        )

        if self.seed is not None:
            np.random.seed(self.seed)
            torch.manual_seed(self.seed)
            if torch.backends.mps.is_available():
                torch.mps.manual_seed(self.seed)
            if torch.cuda.is_available():
                torch.cuda.manual_seed_all(self.seed)
                torch.backends.cudnn.deterministic = True
                torch.backends.cudnn.benchmark = False

        self.ALSTM_model = ALSTMModel(
            d_feat=self.d_feat,
            hidden_size=self.hidden_size,
            num_layers=self.num_layers,
            dropout=self.dropout,
        )
        self.logger.info("model:\n{:}".format(self.ALSTM_model))
        self.logger.info("model size: {:.4f} MB".format(count_parameters(self.ALSTM_model)))

        if optimizer.lower() == "adam":
            self.train_optimizer = optim.Adam(self.ALSTM_model.parameters(), lr=self.lr)
        elif optimizer.lower() == "gd":
            self.train_optimizer = optim.SGD(self.ALSTM_model.parameters(), lr=self.lr)
        else:
            raise NotImplementedError("optimizer {} is not supported!".format(optimizer))

        self.kwargs = kwargs
        self.init_model_path = init_model_path
        if init_model_path is not None and os.path.exists(init_model_path):
            self.logger.info(f"Loading model weights from {init_model_path}")
            self.ALSTM_model.load_state_dict(torch.load(init_model_path, map_location=self.device))
            self.fitted = True
        else:
            self.fitted = False
        self.ALSTM_model.to(self.device)

    @property
    def use_gpu(self):
        return self.device != torch.device("cpu")

    def mse(self, pred, label, weight):
        loss = weight * (pred - label) ** 2
        return torch.mean(loss)

    def loss_fn(self, pred, label, weight=None):
        mask = ~torch.isnan(label)

        if weight is None:
            weight = torch.ones_like(label)

        if self.loss == "mse":
            return self.mse(pred[mask], label[mask], weight[mask])

        raise ValueError("unknown loss `%s`" % self.loss)

    def metric_fn(self, pred, label):
        mask = torch.isfinite(label)

        if self.metric in ("", "loss"):
            return -self.loss_fn(pred[mask], label[mask])
        elif self.metric == "mse":
            mask = ~torch.isnan(label)
            weight = torch.ones_like(label)
            return -self.mse(pred[mask], label[mask], weight[mask])

        raise ValueError("unknown metric `%s`" % self.metric)

    def train_epoch(self, data_loader):
        self.ALSTM_model.train()

        for data, weight in data_loader:
            if self.device == torch.device("mps"):
                if data.dtype != torch.float32:
                    data = data.float()
                if weight.dtype != torch.float32:
                    weight = weight.float()
            feature = data[:, :, 0:-1].to(self.device)
            label = data[:, -1, -1].to(self.device)

            pred = self.ALSTM_model(feature.float())
            loss = self.loss_fn(pred, label, weight.to(self.device))

            self.train_optimizer.zero_grad()
            loss.backward()
            torch.nn.utils.clip_grad_value_(self.ALSTM_model.parameters(), 3.0)
            self.train_optimizer.step()

    def test_epoch(self, data_loader):
        self.ALSTM_model.eval()

        scores = []
        losses = []

        for data, weight in data_loader:
            if self.device == torch.device("mps"):
                if data.dtype != torch.float32:
                    data = data.float()
                if weight.dtype != torch.float32:
                    weight = weight.float()
            feature = data[:, :, 0:-1].to(self.device)
            # feature[torch.isnan(feature)] = 0
            label = data[:, -1, -1].to(self.device)

            with torch.no_grad():
                pred = self.ALSTM_model(feature.float())
                loss = self.loss_fn(pred, label, weight.to(self.device))
                losses.append(loss.item())

                score = self.metric_fn(pred, label)
                scores.append(score.item())

        return np.mean(losses), np.mean(scores)

    def fit(
        self,
        dataset,
        evals_result=dict(),
        save_path=None,
        reweighter=None,
    ):
        save_path = save_path or self.kwargs.get("save_path", None) if self.kwargs else None
        self.logger.info("fit params save_path:%s:", save_path)
        dl_train = dataset.prepare("train", col_set=["feature", "label"], data_key=DataHandlerLP.DK_L)
        dl_valid = dataset.prepare("valid", col_set=["feature", "label"], data_key=DataHandlerLP.DK_L)
        if dl_train.empty or dl_valid.empty:
            raise ValueError("Empty data from dataset, please check your dataset config.")

        dl_train.config(fillna_type="ffill+bfill")  # process nan brought by dataloader
        dl_valid.config(fillna_type="ffill+bfill")  # process nan brought by dataloader

        if reweighter is None:
            wl_train = np.ones(len(dl_train))
            wl_valid = np.ones(len(dl_valid))
        elif isinstance(reweighter, Reweighter):
            wl_train = reweighter.reweight(dl_train)
            wl_valid = reweighter.reweight(dl_valid)
        else:
            raise ValueError("Unsupported reweighter type.")

        train_loader = DataLoader(
            ConcatDataset(dl_train, wl_train),
            batch_size=self.batch_size,
            shuffle=True,
            num_workers=self.n_jobs,
            drop_last=True,
            generator = torch.Generator().manual_seed(self.seed) if self.seed is not None else None,
        )
        valid_loader = DataLoader(
            ConcatDataset(dl_valid, wl_valid),
            batch_size=self.batch_size,
            shuffle=False,
            num_workers=self.n_jobs,
            drop_last=True,
        )

        save_path = get_or_create_path(save_path, return_dir=True)
        model_save_dir = os.path.join(save_path, "model_ckpt")
        os.makedirs(model_save_dir, exist_ok=True)
        # 记录 artifact 到 MLflow
        from qlib.workflow import R
        recorder = R.get_recorder()

        stop_steps = 0
        train_loss = 0
        best_score = -np.inf
        best_epoch = 0
        evals_result["train"] = []
        evals_result["valid"] = []

        # train
        self.logger.info("training...")
        self.fitted = True

        for step in range(self.n_epochs):
            self.logger.info("Epoch%d:", step)
            self.logger.info("training...")
            self.train_epoch(train_loader)
            self.logger.info("evaluating...")
            train_loss, train_score = self.test_epoch(train_loader)
            val_loss, val_score = self.test_epoch(valid_loader)
            self.logger.info("train %.6f, valid %.6f" % (train_score, val_score))
            evals_result["train"].append(train_score)
            evals_result["valid"].append(val_score)
            # ✅ 写入 logger（写入 MLflow）
            if recorder is not None:
                log_m = {"train_loss": train_loss,
                         "val_loss": val_loss,
                         "train_score": train_score,
                         "val_score": val_score
                         }
                recorder.log_metrics(step=step, **log_m)

            # 每轮保存模型
            step_model_path = os.path.join(model_save_dir, f"model_{step}_params.pt")
            torch.save(self.ALSTM_model.state_dict(), step_model_path)
            if recorder is not None:
                recorder.log_artifact(step_model_path, artifact_path="models")

            if val_score > best_score:
                best_score = val_score
                stop_steps = 0
                best_epoch = step
                best_param = copy.deepcopy(self.ALSTM_model.state_dict())
            else:
                stop_steps += 1
                if stop_steps >= self.early_stop:
                    self.logger.info("early stop")
                    break

        self.logger.info("best score: %.6lf @ %d" % (best_score, best_epoch))
        self.ALSTM_model.load_state_dict(best_param)
        # 保存最优模型
        best_model_path = os.path.join(model_save_dir, f"base_model_params.pt")
        torch.save(best_param, best_model_path)
        if recorder is not None:
            recorder.log_artifact(best_model_path, artifact_path="models")

        if self.use_gpu:
            torch.cuda.empty_cache()

    def predict(self, dataset: DatasetH, segment: Union[Text, slice] = "test"):
        if not self.fitted:
            raise ValueError("model is not fitted yet!")

        dl_test = dataset.prepare(segment, col_set=["feature", "label"], data_key=DataHandlerLP.DK_I)
        dl_test.config(fillna_type="ffill+bfill")
        test_loader = DataLoader(dl_test, batch_size=self.batch_size, num_workers=self.n_jobs)
        self.ALSTM_model.eval()
        preds = []

        for data in test_loader:
            feature = data[:, :, 0:-1].to(self.device)

            with torch.no_grad():
                pred = self.ALSTM_model(feature.float()).detach().cpu().numpy()

            preds.append(pred)

        return pd.Series(np.concatenate(preds), index=dl_test.get_index())


class ALSTMModel(nn.Module):
    def __init__(self, d_feat=6, hidden_size=64, num_layers=2, dropout=0.0, rnn_type="GRU"):
        super().__init__()
        self.hid_size = hidden_size
        self.input_size = d_feat
        self.dropout = dropout
        self.rnn_type = rnn_type
        self.rnn_layer = num_layers
        self._build_model()

    def _build_model(self):
        try:
            klass = getattr(nn, self.rnn_type.upper())
        except Exception as e:
            raise ValueError("unknown rnn_type `%s`" % self.rnn_type) from e
        self.net = nn.Sequential()
        self.net.add_module("fc_in", nn.Linear(in_features=self.input_size, out_features=self.hid_size))
        self.net.add_module("act", nn.Tanh())
        self.rnn = klass(
            input_size=self.hid_size,
            hidden_size=self.hid_size,
            num_layers=self.rnn_layer,
            batch_first=True,
            dropout=self.dropout,
        )
        self.fc_out = nn.Linear(in_features=self.hid_size * 2, out_features=1)
        self.att_net = nn.Sequential()
        self.att_net.add_module(
            "att_fc_in",
            nn.Linear(in_features=self.hid_size, out_features=int(self.hid_size / 2)),
        )
        self.att_net.add_module("att_dropout", torch.nn.Dropout(self.dropout))
        self.att_net.add_module("att_act", nn.Tanh())
        self.att_net.add_module(
            "att_fc_out",
            nn.Linear(in_features=int(self.hid_size / 2), out_features=1, bias=False),
        )
        self.att_net.add_module("att_softmax", nn.Softmax(dim=1))

    def forward(self, inputs):
        rnn_out, _ = self.rnn(self.net(inputs))  # [batch, seq_len, num_directions * hidden_size]
        attention_score = self.att_net(rnn_out)  # [batch, seq_len, 1]
        out_att = torch.mul(rnn_out, attention_score)
        out_att = torch.sum(out_att, dim=1)
        out = self.fc_out(
            torch.cat((rnn_out[:, -1, :], out_att), dim=1)
        )  # [batch, seq_len, num_directions * hidden_size] -> [batch, 1]
        return out[..., 0]

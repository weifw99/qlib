# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.


from __future__ import division
from __future__ import print_function

import os
from typing import Union, Text

import numpy as np
import pandas as pd
import copy
import math
from ...utils import get_or_create_path
from ...log import get_module_logger

import torch
import torch.nn as nn
import torch.optim as optim
from torch.utils.data import DataLoader

from ...model.base import Model
from ...data.dataset import DatasetH
from ...data.dataset.handler import DataHandlerLP


class TransformerModel(Model):
    def __init__(
        self,
        d_feat: int = 20,
        d_model: int = 64,
        batch_size: int = 8192,
        nhead: int = 2,
        num_layers: int = 2,
        dropout: float = 0,
        n_epochs=100,
        lr=0.0001,
        metric="",
        early_stop=5,
        loss="mse",
        optimizer="adam",
        reg=1e-3,
        n_jobs=10,
        GPU=0,
        seed=None,
        init_model_path=None,
        **kwargs,
    ):
        # set hyper-parameters.
        self.d_model = d_model
        self.dropout = dropout
        self.n_epochs = n_epochs
        self.lr = lr
        self.reg = reg
        self.metric = metric
        self.batch_size = batch_size
        self.early_stop = early_stop
        self.optimizer = optimizer.lower()
        self.loss = loss
        self.n_jobs = n_jobs
        self.device = torch.device("cuda:%d" % (GPU) if torch.cuda.is_available() and GPU >= 0 else "mps" if torch.backends.mps.is_available() else "cpu")
        self.seed = seed
        self.logger = get_module_logger("TransformerModel")
        self.logger.info("Naive Transformer:" "\nbatch_size : {}" "\ndevice : {}".format(self.batch_size, self.device))

        if self.seed is not None:
            np.random.seed(self.seed)
            torch.manual_seed(self.seed)
            if torch.backends.mps.is_available():
                torch.mps.manual_seed(self.seed)
            if torch.cuda.is_available():
                torch.cuda.manual_seed_all(self.seed)
                torch.backends.cudnn.deterministic = True
                torch.backends.cudnn.benchmark = False

        self.model = Transformer(d_feat, d_model, nhead, num_layers, dropout, self.device)
        if optimizer.lower() == "adam":
            self.train_optimizer = optim.Adam(self.model.parameters(), lr=self.lr, weight_decay=self.reg)
        elif optimizer.lower() == "gd":
            self.train_optimizer = optim.SGD(self.model.parameters(), lr=self.lr, weight_decay=self.reg)
        else:
            raise NotImplementedError("optimizer {} is not supported!".format(optimizer))

        self.kwargs = kwargs
        self.init_model_path = init_model_path
        if init_model_path is not None and os.path.exists(init_model_path):
            self.logger.info(f"Loading model weights from {init_model_path}")
            self.model.load_state_dict(torch.load(init_model_path, map_location=self.device))
            self.fitted = True
        else:
            self.fitted = False
        self.model.to(self.device)

    @property
    def use_gpu(self):
        return self.device != torch.device("cpu")

    def mse(self, pred, label):
        loss = (pred.float() - label.float()) ** 2
        return torch.mean(loss)

    def loss_fn(self, pred, label):
        mask = ~torch.isnan(label)

        if self.loss == "mse":
            return self.mse(pred[mask], label[mask])

        raise ValueError("unknown loss `%s`" % self.loss)

    def metric_fn(self, pred, label):
        mask = torch.isfinite(label)

        if self.metric in ("", "loss"):
            return -self.loss_fn(pred[mask], label[mask])

        raise ValueError("unknown metric `%s`" % self.metric)

    def train_epoch(self, data_loader):
        self.model.train()

        for data in data_loader:
            if self.device == torch.device("mps"):
                if data.dtype != torch.float32:
                    data = data.float()
            feature = data[:, :, 0:-1].to(self.device)
            label = data[:, -1, -1].to(self.device)

            pred = self.model(feature.float())  # .float()
            loss = self.loss_fn(pred, label)

            self.train_optimizer.zero_grad()
            loss.backward()
            torch.nn.utils.clip_grad_value_(self.model.parameters(), 3.0)
            self.train_optimizer.step()

    def test_epoch(self, data_loader):
        self.model.eval()

        scores = []
        losses = []

        for data in data_loader:
            if self.device == torch.device("mps"):
                if data.dtype != torch.float32:
                    data = data.float()
            feature = data[:, :, 0:-1].to(self.device)
            label = data[:, -1, -1].to(self.device)

            with torch.no_grad():
                pred = self.model(feature.float())  # .float()
                loss = self.loss_fn(pred, label)
                losses.append(loss.item())

                score = self.metric_fn(pred, label)
                scores.append(score.item())

        return np.mean(losses), np.mean(scores)

    def fit(
        self,
        dataset: DatasetH,
        evals_result=dict(),
        save_path=None,
    ):
        save_path = save_path or self.kwargs.get("save_path", None) if self.kwargs else None
        self.logger.info("fit params save_path:%s:", save_path)
        dl_train = dataset.prepare("train", col_set=["feature", "label"], data_key=DataHandlerLP.DK_L)
        dl_valid = dataset.prepare("valid", col_set=["feature", "label"], data_key=DataHandlerLP.DK_L)

        if dl_train.empty or dl_valid.empty:
            raise ValueError("Empty data from dataset, please check your dataset config.")

        dl_train.config(fillna_type="ffill+bfill")  # process nan brought by dataloader
        dl_valid.config(fillna_type="ffill+bfill")  # process nan brought by dataloader

        train_loader = DataLoader(
            dl_train, batch_size=self.batch_size, shuffle=True, num_workers=self.n_jobs, drop_last=True, generator = torch.Generator().manual_seed(self.seed) if self.seed is not None else None,
        )
        valid_loader = DataLoader(
            dl_valid, batch_size=self.batch_size, shuffle=False, num_workers=self.n_jobs, drop_last=True
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
            torch.save(self.model.state_dict(), step_model_path)
            if recorder is not None:
                recorder.log_artifact(step_model_path, artifact_path="models")

            if val_score > best_score:
                best_score = val_score
                stop_steps = 0
                best_epoch = step
                best_param = copy.deepcopy(self.model.state_dict())
            else:
                stop_steps += 1
                if stop_steps >= self.early_stop:
                    self.logger.info("early stop")
                    break

        self.logger.info("best score: %.6lf @ %d" % (best_score, best_epoch))
        self.model.load_state_dict(best_param)
        # 保存最优模型
        best_model_path = os.path.join(model_save_dir, f"base_model_params.pt")
        torch.save(best_param, best_model_path)
        if recorder is not None:
            recorder.log_artifact(best_model_path, artifact_path="models")

        if self.use_gpu:
            torch.cuda.empty_cache()

    def predict(self, dataset, segment: Union[Text, slice] = "test"):
        if not self.fitted:
            raise ValueError("model is not fitted yet!")

        dl_test = dataset.prepare(segment, col_set=["feature", "label"], data_key=DataHandlerLP.DK_I)
        dl_test.config(fillna_type="ffill+bfill")
        test_loader = DataLoader(dl_test, batch_size=self.batch_size, num_workers=self.n_jobs)
        self.model.eval()
        preds = []

        for data in test_loader:
            feature = data[:, :, 0:-1].to(self.device)

            with torch.no_grad():
                pred = self.model(feature.float()).detach().cpu().numpy()

            preds.append(pred)

        return pd.Series(np.concatenate(preds), index=dl_test.get_index())


class PositionalEncoding(nn.Module):
    def __init__(self, d_model, max_len=1000):
        super(PositionalEncoding, self).__init__()
        pe = torch.zeros(max_len, d_model)
        position = torch.arange(0, max_len, dtype=torch.float).unsqueeze(1)
        div_term = torch.exp(torch.arange(0, d_model, 2).float() * (-math.log(10000.0) / d_model))
        pe[:, 0::2] = torch.sin(position * div_term)
        pe[:, 1::2] = torch.cos(position * div_term)
        pe = pe.unsqueeze(0).transpose(0, 1)
        self.register_buffer("pe", pe)

    def forward(self, x):
        # [T, N, F]
        return x + self.pe[: x.size(0), :]


class Transformer(nn.Module):
    def __init__(self, d_feat=6, d_model=8, nhead=4, num_layers=2, dropout=0.5, device=None):
        super(Transformer, self).__init__()
        self.feature_layer = nn.Linear(d_feat, d_model)
        self.pos_encoder = PositionalEncoding(d_model)
        self.encoder_layer = nn.TransformerEncoderLayer(d_model=d_model, nhead=nhead, dropout=dropout)
        self.transformer_encoder = nn.TransformerEncoder(self.encoder_layer, num_layers=num_layers)
        self.decoder_layer = nn.Linear(d_model, 1)
        self.device = device
        self.d_feat = d_feat

    def forward(self, src):
        # src [N, T, F], [512, 60, 6]
        src = self.feature_layer(src)  # [512, 60, 8]

        # src [N, T, F] --> [T, N, F], [60, 512, 8]
        src = src.transpose(1, 0)  # not batch first

        mask = None

        src = self.pos_encoder(src)
        output = self.transformer_encoder(src, mask)  # [60, 512, 8]

        # [T, N, F] --> [N, T*F]
        output = self.decoder_layer(output.transpose(1, 0)[:, -1, :])  # [512, 1]

        return output.squeeze()

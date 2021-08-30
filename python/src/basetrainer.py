import os

import cv2
import numpy as np
import torch
import torchmetrics
from torch.utils.data import DataLoader
from tqdm import tqdm
from torch.utils.tensorboard import SummaryWriter

from src.netutils import get_points, make_prob_map_from_labels, make_points_labels
from src.saveutils import load_last_checkpoint, save_checkpoint


class BaseTrainer(object):
    def __init__(self, settings, checkpoint_path, train_dataset, test_dataset):
        self.settings = settings
        self.checkpoint_path = checkpoint_path
        self.learning_rate = self.settings.learning_rate
        self.epochs = self.settings.epochs
        self.summary_writer = SummaryWriter(log_dir=os.path.join(checkpoint_path, 'runs'))
        self.optimizer = None
        self.f1 = 0
        self.global_train_index = 0
        self.last_image = None
        self.last_prob_map = None
        self.last_labels = None
        self.last_warped_image = None
        self.last_warped_prob_map = None
        self.last_warped_labels = None
        self.last_valid_mask = None
        print(f'Trainer is initialized with batch size = {self.settings.batch_size}')
        print(f'Gradient accumulation batch size divider = {self.settings.batch_size_divider}')
        print(f'Automatic Mixed Precision = {self.settings.use_amp}')

        batch_size = self.settings.batch_size // self.settings.batch_size_divider
        self.train_dataset = train_dataset
        self.test_dataset = test_dataset

        self.train_dataloader = DataLoader(self.train_dataset, batch_size=batch_size, shuffle=True,
                                           num_workers=self.settings.data_loader_num_workers)
        self.test_dataloader = DataLoader(self.test_dataset, batch_size=batch_size, shuffle=True,
                                          num_workers=self.settings.data_loader_num_workers)

        self.scaler = torch.cuda.amp.GradScaler()
        self.model = None
        self.softmax = torch.nn.Softmax(dim=1)
        self.f1_metric = torchmetrics.F1(num_classes=65, mdmc_average='samplewise')

    def add_model_graph(self, model):
        fake_input = torch.ones((1, 1, self.settings.train_image_size[0], self.settings.train_image_size[1]),
                                dtype=torch.float32)
        if self.settings.cuda:
            fake_input = fake_input.cuda()
        self.summary_writer.add_graph(model, fake_input)
        self.summary_writer.flush()

    def add_mask_image_summary(self, name, mask, labels, prob_map):
        img_h = prob_map.shape[1]
        img_w = prob_map.shape[2]
        points = get_points(prob_map[0, :, :].unsqueeze(dim=0).cpu(), img_h, img_w, self.settings)
        points = points.T
        points[:, [0, 1]] = points[:, [1, 0]]
        predictions = make_points_labels(points, img_h, img_w, self.settings.cell)

        frame_predictions = (predictions != 64)
        frame_labels = (labels[0, :, :] != 64).cpu().numpy()
        frame = mask[0, 0, :, :].cpu().numpy()
        res_img = (np.dstack((frame, frame_labels, frame_predictions)) * 255.).astype('uint8')
        self.summary_writer.add_image(f'Detector {name} result/train', res_img.transpose([2, 0, 1]), self.global_train_index)

    def add_image_summary(self, name, image, prob_map, labels):
        img_h = image.shape[2]
        img_w = image.shape[3]
        points = get_points(prob_map[0, :, :].unsqueeze(dim=0).cpu(), img_h, img_w, self.settings)
        true_prob_map = make_prob_map_from_labels(labels[0, :, :].cpu().numpy(), img_h, img_w,
                                                  self.settings.cell)
        true_points = get_points(true_prob_map[0, :, :].unsqueeze(dim=0), img_h, img_w, self.settings)
        frame = image[0, 0, :, :].cpu().numpy()
        res_img = (np.dstack((frame, frame, frame)) * 255.).astype('uint8')
        for point in points.T:
            point_int = (int(round(point[0])), int(round(point[1])))
            cv2.circle(res_img, point_int, 3, (255, 0, 0), -1, lineType=16)
        for point in true_points.T:
            point_int = (int(round(point[0])), int(round(point[1])))
            cv2.circle(res_img, point_int, 1, (0, 255, 0), -1, lineType=16)
        self.summary_writer.add_image(f'Detector {name} result/train', res_img.transpose([2, 0, 1]), self.global_train_index)

    def train_loop(self):
        train_loss = torch.tensor(0., device='cuda' if self.settings.cuda else 'cpu')
        batch_loss = torch.tensor(0., device='cuda' if self.settings.cuda else 'cpu')
        divider = torch.tensor(self.settings.batch_size_divider, device='cuda' if self.settings.cuda else 'cpu')
        real_batch_index = 0
        for batch_index, batch in enumerate(tqdm(self.train_dataloader)):
            with torch.set_grad_enabled(True):
                if self.settings.use_amp:
                    with torch.cuda.amp.autocast():
                        loss = self.train_loss_fn(*batch)
                        # normalize loss to account for batch accumulation
                        loss /= divider
                        train_loss += loss
                        batch_loss += loss

                    # Scales the loss, and calls backward()
                    # to create scaled gradients
                    self.scaler.scale(loss).backward()
                else:
                    loss = self.train_loss_fn(*batch)
                    # normalize loss to account for batch accumulation
                    loss /= divider
                    train_loss += loss
                    batch_loss += loss
                    loss.backward()

                # gradient accumulation
                if ((batch_index + 1) % self.settings.batch_size_divider == 0) or (
                        batch_index + 1 == len(self.train_dataloader)):
                    # save statistics
                    if self.settings.write_statistics:
                        self.write_batch_statistics(batch_loss, real_batch_index)

                    # Optimizer step - apply gradients
                    if self.settings.use_amp:
                        # Unscales gradients and calls or skips optimizer.step()
                        self.scaler.step(self.optimizer)
                        # Updates the scale for next iteration
                        self.scaler.update()
                    else:
                        self.optimizer.step()

                    # Clear gradients
                    # This does not zero the memory of each individual parameter,
                    # also the subsequent backward pass uses assignment instead of addition to store gradients,
                    # this reduces the number of memory operations -compared to optimizer.zero_grad()
                    self.optimizer.zero_grad(set_to_none=True)

                    real_batch_index += 1
                    batch_loss.fill_(0.)

        train_loss = train_loss.item() / real_batch_index
        return train_loss

    def test_loop(self):
        test_loss = 0
        batches_num = 0
        with torch.no_grad():
            for batch_index, batch in enumerate(tqdm(self.test_dataloader)):
                loss_value, logits = self.test_loss_fn(*batch)

                softmax_result = self.softmax(logits)
                # normalize metric to account for batch accumulation
                self.f1 += self.f1_metric(softmax_result.cpu(), batch[1].cpu()) / self.settings.batch_size_divider

                # normalize loss to account for batch accumulation
                loss_value /= self.settings.batch_size_divider
                test_loss += loss_value.item()

                if ((batch_index + 1) % self.settings.batch_size_divider == 0) or (
                        batch_index + 1 == len(self.train_dataloader)):
                    batches_num += 1

        test_loss /= batches_num
        return test_loss, batches_num

    def train(self, name, model):
        self.model = model
        self.add_model_graph(model)

        self.optimizer = torch.optim.Adam(self.model.parameters(), lr=self.settings.learning_rate)

        # continue training starting from the latest epoch checkpoint
        start_epoch = 0
        prev_epoch = load_last_checkpoint(self.checkpoint_path, self.model, self.optimizer, self.scaler)
        check_point_loaded = False
        if prev_epoch >= 0:
            check_point_loaded = True
            start_epoch = prev_epoch + 1
        epochs_num = start_epoch + self.epochs

        self.train_init(check_point_loaded)

        self.global_train_index = 0

        for epoch in range(start_epoch, epochs_num):
            print(f"Epoch {epoch}\n-------------------------------")
            train_loss = self.train_loop()
            print(f"Train Loss:{train_loss:7f} \n")
            if self.settings.write_statistics:
                self.summary_writer.add_scalar('Loss/train', train_loss, epoch)

            self.f1 = 0
            self.last_image = None
            self.last_prob_map = None
            test_loss, batches_num = self.test_loop()
            self.f1 /= batches_num

            print(f"Test Loss:{test_loss:7f} \n")
            print(f"Test F1:{self.f1:7f} \n")
            if self.settings.write_statistics:
                self.summary_writer.add_scalar('Loss/test', test_loss, epoch)
                self.summary_writer.add_scalar('F1/test', self.f1, epoch)

            save_checkpoint(name, epoch, model, self.optimizer, self.scaler, self.checkpoint_path)

    def write_batch_statistics(self, loss, batch_index):
        if batch_index % 100 == 0:
            loss_value = loss.item()
            print(f"loss: {loss_value:>7f}")
            self.summary_writer.add_scalar('Loss/train', loss_value, self.global_train_index)

            for name, param in self.model.named_parameters():
                if param.grad is not None and 'bn' not in name:
                    self.summary_writer.add_histogram(
                        tag=f"params/{name}", values=param, global_step=self.global_train_index
                    )
                    self.summary_writer.add_histogram(
                        tag=f"grads/{name}", values=param.grad, global_step=self.global_train_index
                    )

            if self.last_image is not None:
                self.add_image_summary('normal', self.last_image, self.last_prob_map, self.last_labels)

            if self.last_warped_image is not None:
                self.add_image_summary('warped', self.last_warped_image, self.last_warped_prob_map,
                                       self.last_warped_labels)
                self.add_mask_image_summary('mask', self.last_valid_mask, self.last_warped_labels,
                                            self.last_warped_prob_map)

            self.global_train_index += 1

    # The following functions should be overwritten in child classes

    def train_init(self, check_point_loaded):
        pass

    def train_loss_fn(self, image, point_labels, warped_image, warped_point_labels, valid_mask, homographies):
        pass

    def test_loss_fn(self, image, point_labels, warped_image, warped_point_labels, valid_mask, homographies):
        pass

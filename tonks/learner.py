import copy
import time

from fastprogress.fastprogress import format_time, master_bar, progress_bar
import numpy as np
import torch


from tonks.learner_utils import _get_loss_functions


class MultiTaskLearner(object):
    """
    Class to encapsulate training and validation steps for a pipeline. Based off the fastai learner.

    Parameters
    ----------
    model: torch.nn.Module
        PyTorch model to use with the Learner
    train_dataloader: MultiDatasetLoader
        dataloader for all of the training data
    val_dataloader: MultiDatasetLoader
        dataloader for all of the validation data
    task_dict: dict
        dictionary with all of the tasks as keys and the number of unique labels as the values
    loss_function_dict: dict
        dictionary where keys are task names and values are supported loss functions.
        Currently supported losses are `categorical_cross_entropy` for multi-class tasks
        or `bce_logits` for multi-label tasks
    """
    def __init__(self, model, train_dataloader, val_dataloader, task_dict, loss_function_dict=None):
        self.model = model
        self.train_dataloader = train_dataloader
        self.val_dataloader = val_dataloader
        self.task_dict = task_dict
        self.tasks = [*task_dict]
        self.loss_function_dict = _get_loss_functions(loss_function_dict, self.tasks)

    def fit(
        self,
        num_epochs,
        scheduler,
        step_scheduler_on_batch,
        optimizer,
        device='cuda:0',
        best_model=False
    ):
        """
        Fit the PyTorch model

        Parameters
        ----------
        num_epochs: int
            number of epochs to train
        scheduler: torch.optim.lr_scheduler
            PyTorch learning rate scheduler
        step_scheduler_on_batch: bool
            flag of whether to step scheduler on batch (if True) or on epoch (if False)
        loss_function: function
            function to calculate loss with in model
        optimizer: torch.optim
            PyTorch optimzer
        device: str (defaults to 'cuda:0')
            device to run calculations on
        best_model: bool (defaults to `False`)
            flag to save best model from a single `fit` training run based on validation loss
            The default is `False`, which will keep the final model from the training run.
            `True` will keep the best model from the training run instead of the model
            from the final epoch of the training cycle.
        """
        self.model = self.model.to(device)

        current_best_loss = np.iinfo(np.intp).max

        pbar = master_bar(range(num_epochs))
        headers = ['train_loss', 'val_loss']
        for task in self.tasks:
            headers.append(f'{task}_train_loss')
            headers.append(f'{task}_val_loss')
            headers.append(f'{task}_acc')
        headers.append('time')
        pbar.write(headers, table=True)

        for epoch in pbar:
            start_time = time.time()
            self.model.train()

            training_loss_dict = {task: 0.0 for task in self.tasks}

            overall_training_loss = 0.0

            for step, batch in enumerate(progress_bar(self.train_dataloader, parent=pbar)):
                task_type, (x, y) = batch
                x = self._return_input_on_device(x, device)
                y = y.to(device)

                num_rows = self._get_num_rows(x)

                output = self.model(x)

                current_loss = self.loss_function_dict[task_type]['loss'](output[task_type], y)

                scaled_loss = current_loss.item() * num_rows

                training_loss_dict[task_type] += scaled_loss

                overall_training_loss += scaled_loss

                optimizer.zero_grad()
                current_loss.backward()
                optimizer.step()
                if step_scheduler_on_batch:
                    scheduler.step()

            if not step_scheduler_on_batch:
                scheduler.step()

            overall_training_loss = overall_training_loss/self.train_dataloader.total_samples

            for task in self.tasks:
                training_loss_dict[task] = (
                    training_loss_dict[task]
                    / len(self.train_dataloader.loader_dict[task].dataset)
                )

            overall_val_loss, val_loss_dict, accuracies = self.validate(
                device,
                pbar
            )

            str_stats = []
            stats = [overall_training_loss, overall_val_loss]
            for stat in stats:
                str_stats.append(
                    'NA' if stat is None else str(stat) if isinstance(stat, int) else f'{stat:.6f}'
                )

            for task in self.tasks:
                str_stats.append(f'{training_loss_dict[task]:.6f}')
                str_stats.append(f'{val_loss_dict[task]:.6f}')
                try:
                    str_stats.append(f"{accuracies[task]['accuracy']:.6f}")
                except ValueError:
                    str_stats.append(f"{accuracies[task]['accuracy']}")

            str_stats.append(format_time(time.time() - start_time))

            pbar.write(str_stats, table=True)

            if best_model and overall_val_loss < current_best_loss:
                current_best_loss = overall_val_loss
                best_model_wts = copy.deepcopy(self.model.state_dict())
                best_model_epoch = epoch

        if best_model:
            self.model.load_state_dict(best_model_wts)
            print(f'Epoch {best_model_epoch} best model saved with loss of {current_best_loss}')

    def validate(self, device='cuda:0', pbar=None):
        """
        Evaluate the model on a validation set

        Parameters
        ----------
        loss_function: function
            function to calculate loss with in model
        device: str (defaults to 'cuda:0')
            device to run calculations on
        pbar: fast_progress progress bar (defaults to None)
            parent progress bar for all epochs

        Returns
        -------
        overall_val_loss: float
            overall validation loss for all tasks
        val_loss_dict: dict
            dictionary of validation losses for individual tasks
        accuracies: dict
            accuracy measures for individual tasks
        """
        preds_dict = {} #self._create_preds_dict()

        val_loss_dict = {task: 0.0 for task in self.tasks}
        accuracies = {task: {'accuracy': 0.0} for task in self.tasks}

        overall_val_loss = 0.0

        self.model.eval()

        with torch.no_grad():
            index_dict = {task: 0 for task in self.tasks}
            for step, batch in enumerate(
                progress_bar(self.val_dataloader, parent=pbar, leave=(pbar is not None))
            ):
                task_type, (x, y) = batch
                x = self._return_input_on_device(x, device)

                y = y.to(device)
                
                output = self.model(x)

                current_loss = self.loss_function_dict[task_type]['loss'](output[task_type], y)

                val_loss_dict[task_type] += current_loss
                overall_val_loss += current_loss

                #y_pred = self.loss_function_dict[task_type]['final_layer'](output[task_type])

                y_pred = output[task_type].cpu().numpy()
                y_true = y.cpu().numpy()

                current_index = index_dict[task_type]

                num_rows = self._get_num_rows(x)

                if task_type not in preds_dict:

                    preds_dict[task_type] = {
                    'y_true': y_true,
                    'y_pred': y_pred
                    }

                else:
                    preds_dict[task_type]['y_true'] = np.concatenate((preds_dict[task_type]['y_true'],y_true))
                    preds_dict[task_type]['y_pred'] = np.concatenate((preds_dict[task_type]['y_pred'], y_pred))

                index_dict[task_type] += num_rows

        overall_val_loss = overall_val_loss/self.val_dataloader.total_samples

        for task in self.tasks:
            val_loss_dict[task] = (
                val_loss_dict[task]
                / len(self.val_dataloader.loader_dict[task].dataset)
            )

        '''
        we can unify how we store the data as arrrays in shape of the final layer...
        softmax, sigmoid, custom all are jusr # nodes

        Then in accuracy calculation we can apply the function to every row in the array
        and then do the preprocessing for thee accuracy calculation. return 2 values and use
        those as needed...
        '''
        print(accuracies.keys())
        for task in accuracies.keys():

            y_true = preds_dict[task]['y_true']
            y_raw_pred = preds_dict[task]['y_pred']
            print(task,self.loss_function_dict[task]['acc_func'])#,y_true,y_raw_pred,type(y_raw_pred))

            acc, y_preds = self.loss_function_dict[task]['acc_func'](y_true, y_raw_pred)

            '''
            print(preds_dict[task]['y_pred'])
            tensor_y_pred = torch.from_numpy(preds_dict[task]['y_pred'])
            y_preds = self.loss_function_dict[task_type]['final_layer'](tensor_y_pred).numpy()
            task_preds = (
                self.loss_function_dict[task]
                ['accuracy_pre_processing'](y_preds)
            )
            acc = accuracy_score(preds_dict[task]['y_true'], task_preds)
            '''
            accuracies[task]['accuracy'] = acc
            
        return overall_val_loss, val_loss_dict, accuracies

    def get_val_preds(self, device='cuda:0'):
        """
        Return true labels and predictions for data in self.val_dataloaders

        Parameters
        ----------
        device: str (defaults to 'cuda:0')
            device to run calculations on

        Returns
        -------
        Dictionary with dictionary for each task type:
            'y_true': numpy array of true labels, shape: (num_rows,)
            'y_pred': numpy of array of predicted probabilities: shape (num_rows, num_labels)
        """
        #preds_dict = self._create_preds_dict()
        preds_dict = {}
        self.model = self.model.to(device)
        self.model.eval()

        with torch.no_grad():
            index_dict = {task: 0 for task in self.tasks}
            for step, batch in enumerate(progress_bar(self.val_dataloader, leave=False)):
                task_type, (x, y) = batch
                x = self._return_input_on_device(x, device)

                y = y.to(device)

                output = self.model(x)
                #y_pred = self.loss_function_dict[task_type]['final_layer'](output[task_type])

                y_pred = output[task_type].cpu().numpy()
                y_true = y.cpu().numpy()

                current_index = index_dict[task_type]

                num_rows = self._get_num_rows(x)
                if task_type not in preds_dict:
                    print(task_type)

                    preds_dict[task_type] = {
                    'y_true': y_true,
                    'y_pred': y_pred
                    }
                    #preds_dict[task_type]['y_true'] = y_true
                    #preds_dict[task_type]['y_pred'] = y_pred
                else:
                    preds_dict[task_type]['y_true'] = np.concatenate((preds_dict[task_type]['y_true'],y_true))
                    preds_dict[task_type]['y_pred'] = np.concatenate((preds_dict[task_type]['y_true'], y_true))
                index_dict[task_type] += num_rows

        for task in  self.tasks:
            acc, y_preds = self.loss_function_dict[task_type]['acc_func'](preds_dict[task]['y_true'], preds_dict[task]['y_pred'])
            preds_dict[task_type]['y_pred'] = y_preds

        return preds_dict

    def _return_input_on_device(self, x, device):
        return x.to(device)

    def _get_num_rows(self, x):
        return x.size(0)

    def _create_preds_dict(self):
        preds_dict = {}
        for task in self.tasks:
            print('preds_dict',task)
            current_size = len(self.val_dataloader.loader_dict[task].dataset)
            #if self.loss_function_dict[task]['is_multi_class'] is True:
            
            preds_dict[task] = {
                    'y_true': np.zeros([current_size]),
                    'y_pred': np.zeros([current_size, self.task_dict[task]])
                }
            '''
            #else:
            preds_dict[task] = {
                    'y_true': np.zeros([current_size, self.task_dict[task]]),
                    'y_pred': np.zeros([current_size, self.task_dict[task]])
                }
            '''
        return preds_dict


class MultiInputMultiTaskLearner(MultiTaskLearner):
    """
    Multi Input subclass of MultiTaskLearner class

    Parameters
    ----------
    model: torch.nn.Module
        PyTorch model to use with the Learner
    train_dataloader: MultiDatasetLoader
        dataloader for all of the training data
    val_dataloader: MultiDatasetLoader
        dataloader for all of the validation data
    task_dict: dict
        dictionary with all of the tasks as keys and the number of unique labels as the values

    Notes
    -----
    Multi-input datasets should return x's as a tuple/list so that each element
    can be sent to the appropriate device before being sent to the model
    see tonks.vision.dataset's TonksImageDataset class for an example
    """

    def _return_input_on_device(self, x, device):
        """
        Send all model inputs to the appropriate device (GPU or CPU)
        when the inputs are in a dictionary format.

        Parameters
        ----------
        x: dict
            Output of a dataloader where the dataset generator groups multiple
            model inputs (such as multiple images) into a dictionary. Example
            `{'full_img':some_tensor,'crop_img':some_tensor}`

        """
        for k, v in x.items():
            x[k] = v.to(device)
        return x

    def _get_num_rows(self, x):
        return x[next(iter(x))].size(0)

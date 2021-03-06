#!/usr/bin/python
"""Utility functions for benchmarking online learning"""
from __future__ import division
import numpy as np
import keras
from keras.utils import np_utils

from keras.datasets import mnist
from keras.optimizers import Adam, RMSprop, SGD
import keras.backend as K

import pickle
import gzip

import tensorflow as tf

def ema(decay, prev_val, new_val):
    """Compute exponential moving average.

    Args:
        decay: 'sum' to sum up values, otherwise decay in [0, 1]
        prev_val: previous value of accumulator
        new_val: new value
    Returns:
        updated accumulator
    """
    if decay == 'sum':
        return prev_val + new_val
    return decay * prev_val + (1.0 - decay) * new_val

def leak(decay, prev_val, new_val):
    """Compute leaky integrator.

    Like ema, but expectation value depends on decay time constant.

    Args:
        decay: 'sum' to sum up values, otherwise decay in [0, 1]
        prev_val: previous value of accumulator
        new_val: new value
    Returns:
        updated accumulator
    """
    if decay == 'sum':
        return prev_val + new_val
    return decay * prev_val + new_val

def extract_weight_changes(weights, update_ops):
    """Given a list of weights and Assign ops, identify the change in weights.

    Args:
        weights: list of Variables
        update_ops: list of Assign ops, typically computed using Keras' opt.get_updates()

    Returns:
        list of Tensors containing the weight update for each variable
    """
    name_to_var = {v.name: v.value() for v in weights}
    weight_update_ops = list(filter(lambda x: x.op.inputs[0].name in name_to_var, update_ops))
    nonweight_update_ops = list(filter(lambda x: x.op.inputs[0].name not in name_to_var, update_ops))
    # Make sure that all the weight update ops are Assign ops
    for weight in weight_update_ops:
        if weight.op.type != 'Assign':
            raise ValueError('Update op for weight %s is not of type Assign.'%weight.op.inputs[0].name)
    weight_changes = [(new_w.op.inputs[1] - name_to_var[new_w.op.inputs[0].name]) for new_w, old_w in zip(weight_update_ops, weights)]
    # Recreate the update ops, ensuring that we compute the weight changes before updating the weights
    with tf.control_dependencies(weight_changes):
        new_weight_update_ops = [tf.assign(new_w.op.inputs[0], new_w.op.inputs[1]) for new_w in weight_update_ops]
    return weight_changes, tf.group(*(nonweight_update_ops + new_weight_update_ops))


def compute_updates(opt, loss, weights):
    update_ops = opt.get_updates(weights, [], loss)
    deltas, new_update_op = extract_weight_changes(weights, update_ops)
    grads = tf.gradients(loss, weights)
    # Make sure  that deltas are computed _before_ the weight is updated
    return new_update_op, grads, deltas


def split_dataset_by_labels(X, y, task_labels, nb_classes=None, multihead=False):
    """Split dataset by labels.

    Args:
        X: data
        y: labels
        task_labels: list of list of labels, one for each dataset
        nb_classes: number of classes (used to convert to one-hot)
    Returns:
        List of (X, y) tuples representing each dataset
    """
    if nb_classes is None:
        nb_classes = len(np.unique(y))
    datasets = []
    for labels in task_labels:
        idx = np.in1d(y, labels)
        if multihead:
            label_map = np.arange(nb_classes)
            label_map[labels] = np.arange(len(labels))
            data = X[idx], np_utils.to_categorical(label_map[y[idx]], len(labels))
        else:
            data = X[idx], np_utils.to_categorical(y[idx], nb_classes)
        datasets.append(data)
    return datasets

def split_dataset_randomly(X, y, nb_splits, nb_classes=None):
    """Split dataset by labels.

    Args:
        X: data
        y: labels
        nb_splits: number of splits to return
        task_labels: list of list of labels, one for each dataset
        nb_classes: number of classes (used to convert to one-hot)
    Returns:
        List of (X, y) tuples representing each dataset
    """
    if nb_classes is None:
        nb_classes = len(np.unique(y))
    datasets = []
    idx = range(len(y))
    np.random.shuffle(idx)
    split_size = len(y)//nb_splits
    for i in range(nb_splits):
        data = X[idx[split_size*i:split_size*(i+1)]], np_utils.to_categorical(y[idx[split_size*i:split_size*(i+1)]], nb_classes)
        datasets.append(data)
    return datasets

def get_mnist_variations(dsetnames=['MNIST_Rotated', 'MNIST_Basic'], datashape=(-1,1,28,28), validationset_fraction=0.1, multihead=False):
    """ Uses skdata package to import some MNIST variations

    The following dataset names exist in skdata:
        all = ['MNIST_Basic',
        'MNIST_BackgroundImages',
        'MNIST_BackgroundRandom',
        'MNIST_Rotated',
        'MNIST_Noise1',
        'MNIST_Noise2',
        'MNIST_Noise3',
        'MNIST_Noise4', 
        'MNIST_Noise5', 
        'MNIST_Noise6' ]

    args:
        dsetnames: the names of the data sets from above list
        datashape: tuple with shape of the data (default (-1,1,28,28)
        validationset_fraction: the fraction of data to hold out
        multihead: whether to generate a multihead dataset or a single head one

    returns:
        doublet of training and validation set each being a list of tasks consisting of (X,y) tuples 
    """

    from skdata import larochelle_etal_2007 as L2007
    def dset(name):
        rval = getattr(L2007, name)()
        return rval

    n_tasks = len(dsetnames)
    training_datasets = []
    validation_datasets = []

    for i, dsname in enumerate(dsetnames):
        aa = dset(dsname)
        task = aa.classification_task()
        raw_data, raw_labels = task
        nb_datapoints = len(raw_data)
        label_offset = 0
        if multihead:
            nb_classes = 10*n_tasks
            label_offset = i*10
        else:
            nb_classes = 10
        nb_training_examples = int(nb_datapoints*(1.0-validationset_fraction))
        data = raw_data.reshape(datashape)
        labels = np_utils.to_categorical(raw_labels+label_offset, nb_classes)
        training_datasets.append( (data[:nb_training_examples], labels[:nb_training_examples]) )
        validation_datasets.append( (data[nb_training_examples:], labels[nb_training_examples:]) )

    return training_datasets, validation_datasets

def load_mnist(split='train'):
    (X_train, y_train), (X_test, y_test) = mnist.load_data()
    X_train = X_train.reshape(-1, 784)
    X_test = X_test.reshape(-1, 784)
    X_train = X_train.astype('float32')
    X_test = X_test.astype('float32')
    X_train /= 255
    X_test /= 255

    if split == 'train':
        X, y = X_train, y_train
    else:
        X, y = X_test, y_test
    nb_classes = 10
    y = np_utils.to_categorical(y, nb_classes)
    return X, y

def construct_split_mnist(task_labels,  split='train', multihead=False):
    """Split MNIST dataset by labels.

        Args:
                task_labels: list of list of labels, one for each dataset
                split: whether to use train or testing data

        Returns:
            List of (X, y) tuples representing each dataset
    """
    # Load MNIST data and normalize
    nb_classes = 10
    (X_train, y_train), (X_test, y_test) = mnist.load_data()
    X_train = X_train.reshape(-1, 784)
    X_test = X_test.reshape(-1, 784)
    X_train = X_train.astype('float32')
    X_test = X_test.astype('float32')
    X_train /= 255
    X_test /= 255

    if split == 'train':
        X, y = X_train, y_train
    else:
        X, y = X_test, y_test

    return split_dataset_by_labels(X, y, task_labels, nb_classes, multihead)


def construct_randomly_split_mnist(nb_splits=10, mode='train'):
    """Split MNIST dataset by labels.

        Args:
                nb_splits: numer of splits
                mode: whether to use train or testing data

        Returns:
            List of (X, y) tuples representing each dataset
    """
    # Load MNIST data and normalize
    nb_classes = 10
    (X_train, y_train), (X_test, y_test) = mnist.load_data()
    X_train = X_train.reshape(-1, 784)
    X_test = X_test.reshape(-1, 784)
    X_train = X_train.astype('float32')
    X_test = X_test.astype('float32')
    X_train /= 255
    X_test /= 255

    if mode == 'train':
        X, y = X_train, y_train
    else:
        X, y = X_test, y_test

    return split_dataset_randomly(X, y, nb_splits, nb_classes)

def construct_permute_mnist(num_tasks=2,  split='train', permute_all=False, subsample=1):
    """Create permuted MNIST tasks.

        Args:
                num_tasks: Number of tasks
                split: whether to use train or testing data
                permute_all: When set true also the first task is permuted otherwise it's standard MNIST
                subsample: subsample by so much

        Returns:
            List of (X, y) tuples representing each dataset
    """
    # Load MNIST data and normalize
    nb_classes = 10
    (X_train, y_train), (X_test, y_test) = mnist.load_data()
    X_train = X_train.reshape(-1, 784)
    X_test = X_test.reshape(-1, 784)
    X_train = X_train.astype('float32')
    X_test = X_test.astype('float32')
    X_train /= 255
    X_test /= 255

    X_train, y_train = X_train[::subsample], y_train[::subsample]
    X_test, y_test = X_test[::subsample], y_test[::subsample]

    permutations = []
    # Generate random permutations
    for i in range(num_tasks):
        idx = np.arange(X_train.shape[1],dtype=int)
        if permute_all or i>0:
            np.random.shuffle(idx)
        permutations.append(idx)

    both_datasets = []
    for (X, y) in ((X_train, y_train), (X_test, y_test)):
        datasets = []
        for perm in permutations:
            data = X[:,perm], np_utils.to_categorical(y, nb_classes)
            datasets.append(data)
        both_datasets.append(datasets)
    return both_datasets

def online_benchmark(datasets, model, loss, optimizer, epochs_per_dataset=1,
        ages=1, batch_size=256, callbacks=None, **kwargs):
    """Benchmark online learning.

    Sequentially optimize a set of tasks, and compute
    the predictions for each task over time.

    Args:
        datasets: list of (inputs, labels) tuples
        model: Keras model
        loss: string or function
        optimizer: string or Keras Optimizer object
        epochs_per_dataset: number of passes through an individual dataset
        ages: number of passes over datasets
        batch_size: batch size
        callbacks: list of functions to call with the model at each iteration

    Returns:
        labels:
        predictions:
    """

    # Build the model
    model.compile(loss=loss, optimizer=optimizer, metrics=['accuracy'])

    ndataset = len(datasets)
    predictions = [[] for i in range(ndataset)]
    labels = [[] for i in range(ndataset)]
    if callbacks is not None:
        callback_outputs = [[] for i in range(len(callbacks))]
        for cidx, callback in enumerate(callbacks):
            callback_outputs[cidx].append(callback(model))

    optimization_data = [[] for i in range(len(model.get_weights())) ]
    for age in range(ages):
        for didx, dataset in enumerate(datasets):

            model.fit(*dataset,  batch_size=batch_size, nb_epoch=epochs_per_dataset, verbose=1)
            # Log w, g, g2, ...
            # For all variables, ... ,
            if isinstance(optimizer, Adam):
                weights = model.get_weights()
                opt_vars = optimizer.weights[1:]
                ms = opt_vars[:len(opt_vars)//2]
                vs = opt_vars[len(opt_vars)//2:]
                sess = K.get_session()
                stuff = sess.run([ms, vs])
                for i in range(len(model.get_weights())):
                    optimization_data[i].append([stuff[0][i], stuff[1][i]])
                #optimization_data.append(stuff)

            # Evaluate on all datasets
            for eval_didx, eval_dataset in enumerate(datasets):
                # Evaluate model on dataset
                preds = model.predict(eval_dataset[0])
                predictions[eval_didx].append(preds)
                # Convert from 1-hot back to categorical
                labels[eval_didx].append(np.argmax(eval_dataset[1], 1))
                print(model.evaluate(*eval_dataset))
            print("")
            if callbacks is not None:
                for cidx, callback in enumerate(callbacks):
                    callback_outputs[cidx].append(callback(model))

    if callbacks is None:
        callback_outputs = None
    # TODO(ben): might break some shit


    return dict(labels=labels, predictions=predictions,
                callback_outputs=callback_outputs,
                optimization_data=optimization_data)


def save_zipped_pickle(obj, filename, protocol=-1):
    with gzip.open(filename, 'wb') as f:
        pickle.dump(obj, f, protocol)
        

def load_zipped_pickle(filename):
    try:
        with gzip.open(filename, 'rb') as f:
            loaded_object = pickle.load(f)
            return loaded_object
    except IOError:
        print("Warning: IO Error returning empty dict.")
        return dict()


def split_dataset(ds, split_sizes, permute_data=True):
    """ Helper function to split a single dataset into train, valid and test set. 
    
    args:
        ds the dataset being a tuple of (data,labels)
        split_sizes a list of fractional split sizes of howto divide up the dataset 

    returns:
        a list of datasets with the respective split ratios
    """
    raw_data, raw_labels = ds
    if permute_data:
        idx = range(len(raw_data))
        np.random.shuffle(idx)
        data = raw_data[idx]
        labels = raw_labels[idx]
    else:
        data = raw_data
        labels = raw_labels
    nelems = len(labels)
    nbegin = 0 
    splits = []
    for split in split_sizes:
        nend = nbegin+int(split*nelems)
        splits.append( (data[nbegin:nend], labels[nbegin:nend]) )
        nbegin = nend
    return splits

def mk_training_validation_splits( full_datasets, split_fractions = (0.8, 0.1, 0.1) ):
    """ Splits multiple a list of tasks into training, validation and test sets

    args:
        full_datasets: The full dataset as a list of tasks each being of the form (data, labels)
        split_fractions: A list of split fractions which should sum up to 1.0

    returns:
        a list of length len(split_fractions) each containing a list of tasks
    """
    results = [ [] for i in range(len(split_fractions)) ]
    for ds in full_datasets:
        splits = split_dataset(ds, split_fractions)
        for i,sp in enumerate(splits):
            results[i].append(sp)
    return results

def mk_joined_dataset( full_datasets, split_fractions = (0.9, 0.1) ):
    """ Joins datasets from multiple tasks to a single dataset as a baseline control and returns training and validation splints. """
    l = len(full_datasets)
    data = np.concatenate([ full_datasets[i][0] for i in range(l) ], 0)
    labels = np.concatenate([ full_datasets[i][1] for i in range(l) ], 0)
    return split_dataset((data, labels), split_fractions)



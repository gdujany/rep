"""
This is specific meta-algorithm based on the idea of cross-validation.
"""
from __future__ import division, print_function, absolute_import

import numpy
from sklearn import clone

from six.moves import zip
from . import utils
from sklearn.cross_validation import KFold
from sklearn.utils.validation import check_random_state
from .factory import train_estimator
from ..estimators.interface import Classifier, Regressor
from ..estimators.utils import check_inputs

__author__ = 'Tatiana Likhomanenko, Alex Rogozhnikov'
__all__ = ['FoldingClassifier', 'FoldingRegressor']


def get_regressor_prediction(regressor, data):
    return regressor.predict(data)


def get_regressor_staged_predict(regressor, data):
    return regressor.staged_predict(data)


def get_classifier_probabilities(classifier, data):
    try:
        return classifier.predict_proba(data)
    except AttributeError:
        probabilities = numpy.zeros(shape=(len(data), len(classifier.classes_)))
        labels = classifier.predict(data)
        probabilities[numpy.arange(len(labels)), labels] = 1
        return probabilities


def get_classifier_staged_proba(classifier, data):
    return classifier.staged_predict_proba(data)


class FoldingBase(object):
    """
    Base class for FoldingClassifier and FoldingRegressor
    """

    def __init__(self,
                 base_estimator,
                 n_folds=2,
                 random_state=None,
                 features=None,
                 ipc_profile=None):
        self.estimators = []
        self.ipc_profile = ipc_profile
        self.n_folds = n_folds
        self.base_estimator = base_estimator
        self._folds_indices = None
        self.random_state = random_state
        self._random_number = None
        # setting features directly
        self.features = features

    def _get_folds_column(self, length):
        """
        Return special column with indices of folds for all events.
        """
        if self._random_number is None:
            self._random_number = check_random_state(self.random_state).randint(0, 100000)
        folds_column = numpy.zeros(length)
        for fold_number, (_, folds_indices) in enumerate(
                KFold(length, self.n_folds, shuffle=True, random_state=self._random_number)):
            folds_column[folds_indices] = fold_number
        return folds_column

    def _prepare_data(self, X, y, sample_weight):
        raise NotImplementedError('To be implemented in descendant')

    def fit(self, X, y, sample_weight=None):
        """
        Train the classifier, will train several base classifiers on overlapping
        subsets of training dataset.

        :param X: pandas.DataFrame of shape [n_samples, n_features]
        :param y: labels of events - array-like of shape [n_samples]
        :param sample_weight: weight of events,
               array-like of shape [n_samples] or None if all weights are equal
        """
        if hasattr(self.base_estimator, 'features'):
            assert self.base_estimator.features is None, \
                'Base estimator must have None features! Use features parameter in Folding instead'
        self.train_length = len(X)
        X, y, sample_weight = self._prepare_data(X, y, sample_weight)

        folds_column = self._get_folds_column(len(X))

        for _ in range(self.n_folds):
            self.estimators.append(clone(self.base_estimator))

        if sample_weight is None:
            weights_iterator = [None] * self.n_folds
        else:
            weights_iterator = (sample_weight[folds_column != index] for index in range(self.n_folds))

        result = utils.map_on_cluster(self.ipc_profile, train_estimator,
                                      range(len(self.estimators)),
                                      self.estimators,
                                      (X.iloc[folds_column != index, :].copy() for index in range(self.n_folds)),
                                      (y[folds_column != index] for index in range(self.n_folds)),
                                      weights_iterator)
        for status, data in result:
            if status == 'success':
                name, classifier, spent_time = data
                self.estimators[name] = classifier
            else:
                print('Problem while training on the node, report:\n', data)
        return self

    def _folding_prediction(self, X, prediction_function, vote_function=None):
        """
        Supplementary function to predict (labels, probabilities, values)
        :param X: dataset to predict
        :param prediction_function: function(classifier, X) -> prediction
        :param vote_function: if using averaging over predictions of folds, this function shall be passed.
            For instance: lambda x: numpy.mean(x, axis=0), which means averaging result over all folds.
            Another useful option is lambda x: numpy.median(x, axis=0)
        """
        X = self._get_features(X)
        if vote_function is not None:
            print('KFold prediction with voting function')
            results = []
            for estimator in self.estimators:
                results.append(prediction_function(estimator, X))
            # results: [n_classifiers, n_samples, n_dimensions], reduction over 0th axis
            results = numpy.array(results)
            return vote_function(results)
        else:
            if len(X) != self.train_length:
                print('KFold prediction using random classifier (length of data passed not equal to length of train)')
            else:
                print('KFold prediction using folds column')
            folds_column = self._get_folds_column(len(X))
            parts = []
            for fold in range(self.n_folds):
                parts.append(prediction_function(self.estimators[fold], X.iloc[folds_column == fold, :]))

            result_shape = [len(X)] + list(numpy.shape(parts[0])[1:])
            results = numpy.zeros(shape=result_shape)
            folds_indices = [numpy.where(folds_column == fold)[0] for fold in range(self.n_folds)]
            for fold, part in enumerate(parts):
                results[folds_indices[fold]] = part
            return results

    def _staged_folding_prediction(self, X, prediction_function, vote_function=None):
        X = self._get_features(X)
        if vote_function is not None:
            print('Using voting KFold prediction')
            iterators = [prediction_function(estimator, X) for estimator in self.estimators]
            for fold_prob in zip(*iterators):
                result = numpy.array(fold_prob)
                yield vote_function(result)
        else:
            if len(X) != self.train_length:
                print('KFold prediction using random classifier (length of data passed not equal to length of train)')
            else:
                print('KFold prediction using folds column')
            folds_column = self._get_folds_column(len(X))
            iterators = [prediction_function(self.estimators[fold], X.iloc[folds_column == fold, :])
                         for fold in range(self.n_folds)]
            folds_indices = [numpy.where(folds_column == fold)[0] for fold in range(self.n_folds)]
            for stage_results in zip(*iterators):
                result_shape = [len(X)] + list(numpy.shape(stage_results[0])[1:])
                result = numpy.zeros(result_shape)
                for fold in range(self.n_folds):
                    result[folds_indices[fold]] = stage_results[fold]
                yield result


class FoldingRegressor(FoldingBase, Regressor):
    """
    This meta-regressor implements folding algorithm:

    * training data is splitted into n equal parts;

    * we train n regressors, each one is trained using n-1 folds

    To build unbiased predictions for data, pass the **same** dataset (with same order of events)
    as in training to `predict` or `staged_predict`, in which case
    classifier will use to predict each event that base classifier which didn't use that event during training.

    To use information from not one, but several estimators during predictions,
    provide appropriate voting function. Examples of voting function:
    >>> voting = lambda x: numpy.mean(x, axis=0)
    >>> voting = lambda x: numpy.median(x, axis=0)

    Parameters:
    -----------
    :param sklearn.BaseEstimator base_estimator: base classifier, which will be used for training
    :param int n_folds: count of folds
    :param features: features used in training
    :type features: None or list[str]
    :param ipc_profile: profile for IPython cluster, None to compute locally.
    :type ipc_profile: None or str
    :param random_state: random state for reproducibility
    :type random_state: None or int or RandomState
    """

    def _prepare_data(self, X, y, sample_weight):
        X = self._get_features(X)
        y_shape = numpy.shape(y)
        self.n_outputs_ = 1 if len(y_shape) < 2 else y_shape[1]
        return check_inputs(X, y, sample_weight=sample_weight, allow_multiple_targets=True)

    def predict(self, X, vote_function=None):
        """
        Get predictions. To get unbiased predictions on training dataset, pass training data
        (with same order of events) and vote_function=None.

        :param X: pandas.DataFrame of shape [n_samples, n_features]
        :param vote_function: function to combine prediction of folds' estimators.
            If None then folding scheme is used. Parameters: numpy.ndarray [n_classifiers, n_samples]
        :type vote_function: None or function
        :rtype: numpy.array of shape [n_samples, n_outputs]
        """
        return self._folding_prediction(X, prediction_function=get_regressor_prediction,
                                        vote_function=vote_function)

    def staged_predict(self, X, vote_function=None):
        """
        Get predictions after each iteration of base estimator.
        To get unbiased predictions on training dataset, pass training data
        (with same order of events) and vote_function=None.

        :param X: pandas.DataFrame of shape [n_samples, n_features]
        :param vote_function: function to combine prediction of folds' estimators.
            If None then folding scheme is used. Parameters: numpy.ndarray [n_classifiers, n_samples]
        :type vote_function: None or function
        :rtype: sequence of numpy.array of shape [n_samples, n_outputs]
        """
        return self._folding_prediction(X, prediction_function=get_regressor_staged_predict,
                                        vote_function=vote_function)


class FoldingClassifier(FoldingBase, Classifier):
    """
    This meta-classifier implements folding algorithm:

    * training data is splitted into n equal parts;

    * we train n classifiers, each one is trained using n-1 folds


    To build unbiased predictions for data, pass the **same** dataset (with same order of events)
    as in training to `predict`, `predict_proba` or `staged_predict_proba`, in which case
    classifier will use to predict each event that base classifier which didn't use that event during training.

    To use information from not one, but several estimators during predictions,
    provide appropriate voting function. Examples of voting function:
    >>> voting = lambda x: numpy.mean(x, axis=0)
    >>> voting = lambda x: numpy.median(x, axis=0)

    Parameters:
    -----------
    :param sklearn.BaseEstimator base_estimator: base classifier, which will be used for training
    :param int n_folds: count of folds
    :param features: features used in training
    :type features: None or list[str]
    :param ipc_profile: profile for IPython cluster, None to compute locally.
    :type ipc_profile: None or str
    :param random_state: random state for reproducibility
    :type random_state: None or int or RandomState
    """

    def _prepare_data(self, X, y, sample_weight):
        X = self._get_features(X)
        self._set_classes(y)
        return check_inputs(X, y, sample_weight=sample_weight, allow_multiple_targets=True)

    def predict(self, X, vote_function=None):
        """
        Predict labels. To get unbiased predictions on training dataset, pass training data
        (with same order of events) and vote_function=None.

        :param X: pandas.DataFrame of shape [n_samples, n_features]
        :param vote_function: function to combine prediction of folds' estimators.
            If None then folding scheme is used.
        :type vote_function: None or function
        :rtype: numpy.array of shape [n_samples]
        """
        return numpy.argmax(self.predict_proba(X, vote_function=vote_function), axis=1)

    def predict_proba(self, X, vote_function=None):
        """
        Predict probabilities. To get unbiased predictions on training dataset, pass training data
        (with same order of events) and vote_function=None.

        :param X: pandas.DataFrame of shape [n_samples, n_features]
        :param vote_function: function to combine prediction of folds' estimators.
            If None then folding scheme is used.
        :type vote_function: None or function
        :rtype: numpy.array of shape [n_samples, n_classes]
        """
        result = self._folding_prediction(X, prediction_function=get_classifier_probabilities,
                                          vote_function=vote_function)
        return result / numpy.sum(result, axis=1, keepdims=True)

    def staged_predict_proba(self, X, vote_function=None):
        """
        Predict probabilities after each stage of base_estimator.
        To get unbiased predictions on training dataset, pass training data
        (with same order of events) and vote_function=None.

        :param X: pandas.DataFrame of shape [n_samples, n_features]
        :param vote_function: function to combine prediction of folds' estimators.
            If None then folding scheme is used.
        :type vote_function: None or function
        :rtype: sequence of numpy.arrays of shape [n_samples, n_classes]
        """
        for proba in self._staged_folding_prediction(X, prediction_function=get_classifier_staged_proba,
                                                     vote_function=vote_function):
            yield proba / numpy.sum(proba, axis=1, keepdims=True)


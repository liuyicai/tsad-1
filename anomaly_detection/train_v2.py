from pandas import DataFrame
from sklearn.preprocessing import MinMaxScaler
import os
import random
os.environ['TF_CPP_MIN_LOG_LEVEL'] = '4'
import traceback
from tensorflow.keras.layers import Dense, LSTM, Input
from tensorflow.keras.optimizers import RMSprop
from tensorflow.keras.models import Model
from tensorflow.keras.initializers import Ones
from tensorflow.keras.models import model_from_json
import numpy as np
from itertools import product
import threading
from multiprocessing import cpu_count

from functions import *
from configs import conf, boostrap_ratio, iteration, hyper_conf
from data_access import *
from logger import LoggerProcess


def model_from_to_json(path=None, model=None, is_writing=False):
    if is_writing:
        model_json = model.to_json()
        with open(path, "w") as json_file:
            json_file.write(model_json)
    else:
        json_file = open(path, 'r')
        loaded_model_json = json_file.read()
        json_file.close()
        return model_from_json(loaded_model_json)


def calculate_batch_count(size, parameters):
    batches = int(size / parameters['batch_size'])
    if batches != 0:
        if batches <= 10:
            parameters['batch_count'] = batches - 1  # 2 batches for train, 1 for validation
        if 10 < batches > 10 <= 20:
            parameters['batch_count'] = batches - 2  # n - 2 batches for train, 2 for validation
        if 20 < batches <= 30:
            parameters['batch_count'] = batches - 3  # n - 3 batches for train, 3 for validation
        if batches > 30:
            parameters['batch_count'] = int(batches * parameters['split_ratio'])
    else:
        print("batch_size is updated ", int(size / 2))
        parameters['batch_count'], parameters['batch_size'] = 1, int(size / 2)
    return parameters


def get_tuning_params(parameter_tuning, params, job):
    arrays = []
    for p in params:
        if p not in list(parameter_tuning.keys()):
            arrays.append([params[p]])
        else:
            arrays.append(
                          np.arange(float(parameter_tuning[p].split("*")[0]),
                                    float(parameter_tuning[p].split("*")[1]),
                                    float(parameter_tuning[p].split("*")[0])).tolist()
            )
    comb_arrays = list(product(*arrays))
    if job != 'parameter_tuning':
        return random.sample(comb_arrays, int(len(comb_arrays)*0.5))
    else:
        return comb_arrays


def get_params(params, comb):
    count = 0
    for p in params:
        _p = type(params[p])(comb[count])
        params[p] = _p
        count += 1
    return params


class TrainLSTM:
    def __init__(self,
                 job=None, groups=None, time_indicator=None, feature=None,
                 data_source=None, data_query_path=None, time_period=None):
        self.job = job
        self.params = hyper_conf('lstm')
        self.combination_params = hyper_conf('lstm_cp')
        self.hyper_params = hyper_conf('lstm_pt')
        self.optimized_parameters = {}
        self._p = None
        self.query_date = get_query_date(job, params=self.params)
        self.data, self.groups = data_manipulation(job=job,
                                                   date=self.query_date,
                                                   time_indicator=time_indicator,
                                                   feature=feature,
                                                   data_source=data_source,
                                                   groups=groups,
                                                   data_query_path=data_query_path)
        self.date = time_indicator
        self.f_w_data = self.data
        self.split_date = get_split_date(period=None,
                                         dates=list(self.data[self.date]),
                                         params=self.params)
        self.data_count = 0
        self.feature, self.feature_norm = feature, feature + '_norm'
        self.scale = None
        self.train, self.prediction = None, None
        self.input, self.lstm, self.model = None, None, {}
        self.model = None
        self.result = None, None
        self.residuals, self.anomaly = [], []
        self.sample_size = 30
        self.count = 1
        self.levels = get_levels(self.data, self.groups)
        self.levels_tuning = get_tuning_params(self.hyper_params, self.params, self.job)
        self.logger = LoggerProcess(job=job,
                                    model='lstm',
                                    total_process=len(self.levels)
                                    if job != 'parameter_tuning' else len(self.levels_tuning))
        self.comb = None
        self.is_normalized_feature = check_for_normalization(list(self.data[self.feature]))

    def get_query(self):
        count = 0
        query = ''
        for c in self.comb:
            if type(c) != str:
                query += self.groups[count] + ' == ' + str(c) + ' and '
            else:
                query += self.groups[count] + " == '" + str(c) + "' and "
            count += 1
        query = query[:-4]
        return query

    def get_related_params(self):
        self._p = hyper_conf('lstm') if self.combination_params is None else self.combination_params[self.get_param_key()]

    def normalization(self):
        self.f_w_data[self.feature_norm] = self.f_w_data[self.feature]
        if not self.is_normalized_feature:
            self.scale = MinMaxScaler(feature_range=(0, 1))
            self.f_w_data[self.feature_norm] = self.scale.fit_transform(self.f_w_data[[self.feature]])

    def split_data(self):
        self.result = self.f_w_data[self.f_w_data[
                                        self.date] >= (self.split_date - calculate_intersect_days(self._p))]

    def batch_size(self):
        self.data_count = len(drop_calculation(self.f_w_data, self._p, is_prediction=True))
        self._p = calculate_batch_count(self.data_count, self._p)

    def data_preparation(self, is_prediction):
        if not is_prediction:
            self.train = data_preparation(self.f_w_data, [self.feature_norm], self._p, is_prediction=False)
        else:
            self.prediction = data_preparation(self.result, [self.feature_norm], self._p, is_prediction=True)

    def init_tensorflow(self):
        self.count += 1
        if self.count % 40 == 0:
            from tensorflow.keras.layers import Dense, LSTM, Input
            from tensorflow.keras.optimizers import RMSprop
            from tensorflow.keras.models import Model
            from tensorflow.keras.initializers import Ones
            self.count = 1

    def create_model(self):
        self.init_tensorflow()
        self.input = Input(shape=(self.train['x_train'].shape[1], 1))
        self.lstm = LSTM(self._p['units'],
                         batch_size=self._p['batch_size'],
                         recurrent_initializer=Ones(),
                         kernel_initializer=Ones(),
                         use_bias=False,
                         recurrent_activation=self._p['activation'],
                         dropout=0.25
                         )(self.input)
        self.lstm = Dense(1)(self.lstm)
        self.model = Model(inputs=self.input, outputs=self.lstm)
        self.model.compile(loss='mae', optimizer=RMSprop(lr=self._p['lr']), metrics=['mae'])

    def learning_process(self, save_model=True):
        self.model.fit(self.train['x_train'],
                       self.train['y_train'],
                       batch_size=self._p['batch_size'],
                       epochs=self._p['epochs'],
                       verbose=0,
                       validation_data=(self.train['x_test'], self.train['y_test']),
                       shuffle=False)
        if save_model:
            model_from_to_json(path=join(conf('model_main_path'), model_path(self.comb, self.groups, 'lstm')),
                               model=self.model,
                               is_writing=True)

    def anomaly_prediction(self):
        self.model = model_from_to_json(path=join(conf('model_main_path'), model_path(self.comb, self.groups, 'lstm')))
        self.prediction = self.scale.inverse_transform(self.model.predict(self.prediction))
        self.result = self.result[(len(self.result) - len(self.prediction)):]
        self.result['predict'] = self.prediction.reshape(1, len(self.prediction)).tolist()[0]

    def assign_anomaly_score_and_label(self):
        self.result = self.result.sort_values(by=self.date, ascending=True)
        self.result[self.feature] = self.result[self.feature].fillna(0)
        self.result = self.result.reset_index(drop=True).reset_index()
        self.result['residuals'] = self.result.apply(lambda row:
                                                     calculate_residuals(row[self.feature], row['predict']),
                                                     axis=1)
        try:
            a_score = AnomalyScore(data=self.result,
                                   feature=self.feature,
                                   s_size=get_sample_size(len(self.result)),
                                   iteration=iteration)
            a_score.create_iterations()
            self.result['anomaly_score_1'] = self.result.apply(
                lambda row: a_score.calculate_beta_pdf(value=row['residuals'], idx=row['index']), axis=1)

        except Exception as e:
            print(e)
            a_score = AnomalyScore(data=self.result,
                                   feature=self.feature, s_size=len(self.result))
            self.result['anomaly_score_1'] = a_score.outlier_detection()
        self.result['ad_label_1'] = self.result['anomaly_score_1'].apply(lambda x: 1 if x < 0.05 or x > 0.95 else 0)
        self.anomaly += self.result[['anomaly_score_1', 'ad_label_1',
                                     'residuals', 'predict', self.date] + self.groups].to_dict('results')
        print(self.result[['anomaly_score_1', 'ad_label_1', 'predict'] + self.groups].head())

    def train_execute(self):
        if not hyper_conf('lstm_has_param_tuning_first_run'):
            self.parameter_tuning()
        for self.comb in self.levels:
            print("*" * 4, "LSTM - ", self.get_query().replace(" and ", "; ").replace(" == ", " - "), "*" * 4)
            self.f_w_data = self.data.query(self.get_query()).sort_values(by=self.date)
            self.get_related_params()
            try:
                if len(self.f_w_data) > self._p['batch_size'] * 2:
                    self.normalization()
                    self.batch_size()
                    self.data_preparation(is_prediction=False)
                    self.create_model()
                    self.learning_process()
                self.logger.counter()
                if not check_request_stoped(self.job):
                    break
            except Exception as e:
                print(e)
                traceback.print_exc(file=sys.stdout)

    def prediction_execute(self):
        for self.comb in self.levels:
            self.f_w_data = self.data.query(self.get_query()).sort_values(by=self.date)
            if check_model_exists(model_path(self.comb, self.groups, 'lstm'), conf('model_main_path')):
                try:
                    self.normalization()
                    self.split_data()
                    if len(self.result) != 0:
                        self.data_preparation(is_prediction=True)
                        self.anomaly_prediction()
                        self.assign_anomaly_score_and_label()
                except Exception as e:
                    print(e)
            self.logger.counter()
            if not check_request_stoped(self.job):
                break
        self.anomaly = DataFrame(self.anomaly)

    def process_execute(self, pr, count):
        self.get_related_params()
        self._p = get_params(self._p, pr)
        print("hyper parameters : ", self._p)
        self.normalization()
        self.batch_size()
        self.data_preparation(is_prediction=False)
        self.create_model()
        self.learning_process(save_model=False)
        error[count] = np.mean(self.model.history.history['loss'])

    def parameter_tuning_threading(self, has_comb=True):
        global error
        error = {}
        _optimized_parameters = None
        err = 100000000
        self.f_w_data = self.data.query(self.get_query()).sort_values(by=self.date) if has_comb else self.f_w_data
        for iter in range(int(len(self.levels_tuning) / cpu_count())):
            _levels = self.levels_tuning[(iter * cpu_count()):((iter + 1) * cpu_count())]
            for i in range(len(_levels)):
                self.logger.counter()
                process = threading.Thread(target=self.process_execute, daemon=True, args=(_levels[i], i, ))
                process.start()
            process.join()
            for i in error:
                if i in list(error.keys()):
                    if error[i] < err:
                        err = error[i]
                        _optimized_parameters = get_params(self.params, _levels[i])
                        _optimized_parameters['batch_count'] = self.params['batch_count']
        return _optimized_parameters

    def get_param_key(self):
        return "_".join([str(i[0]) + "*" + str(i[1]) for i in zip(self.groups, self.comb)])

    def parameter_tuning(self):
        if len(self.levels) == 0:
            self.optimized_parameters = self.parameter_tuning_threading(has_comb=False)
        else:
            for self.comb in self.levels:
                self.optimized_parameters[self.get_param_key()] = self.parameter_tuning_threading()
                if not check_request_stoped(self.job):
                    break
        print("updating model parameters")
        pt_config = read_yaml(conf('docs_main_path'), 'parameter_tunning.yaml')
        pt_config['has_param_tuning_first_run']['lstm'] = True
        _key = 'hyper_parameters' if len(self.levels) == 0 else 'combination_params'
        pt_config[_key]['lstm'] = self.optimized_parameters
        write_yaml(conf('docs_main_path'), "parameter_tunning.yaml", pt_config, ignoring_aliases=True)
        self.params = hyper_conf('lstm')
        self.combination_params = hyper_conf('lstm_cp')




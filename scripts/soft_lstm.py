'''
Build a 3 level classifier
'''
from collections import OrderedDict
import cPickle as pkl
import sys
import time

import numpy
import theano
from theano import config
import theano.tensor as tensor
from theano.sandbox.rng_mrg import MRG_RandomStreams as RandomStreams

import nordstrom
import pdb
import copy 

datasets = {'nordstrom': (nordstrom.load_data, nordstrom.prepare_data)}
folder_path = '../'


# Set the random number generators' seeds for consistency
SEED = 123
numpy.random.seed(SEED)

def numpy_floatX(data):
    return numpy.asarray(data, dtype=config.floatX)


def get_minibatches_idx(n, minibatch_size, shuffle=False):
    """
    Used to shuffle the dataset at each iteration.
    """

    idx_list = numpy.arange(n, dtype="int32")

    if shuffle:
        numpy.random.shuffle(idx_list)

    minibatches = []
    minibatch_start = 0
    for i in range(n // minibatch_size):
        minibatches.append(idx_list[minibatch_start:
                                    minibatch_start + minibatch_size])
        minibatch_start += minibatch_size

    if (minibatch_start != n):
        # Make a minibatch out of what is left
        minibatches.append(idx_list[minibatch_start:])

    return zip(range(len(minibatches)), minibatches)


def get_dataset(name):
    return datasets[name][0], datasets[name][1]


def zipp(params, tparams):
    """
    When we reload the model. Needed for the GPU stuff.
    """
    for kk, vv in params.iteritems():
        tparams[kk].set_value(vv)


def unzip(zipped):
    """
    When we pickle the model. Needed for the GPU stuff.
    """
    new_params = OrderedDict()
    for kk, vv in zipped.iteritems():
        new_params[kk] = vv.get_value()
    return new_params


def dropout_layer(state_before, use_noise, trng):
    proj = tensor.switch(use_noise,
                         (state_before *
                          trng.binomial(state_before.shape,
                                        p=0.5, n=1,
                                        dtype=state_before.dtype)),
                         state_before * 0.5)
    return proj


def _p(pp, name):
    return '%s_%s' % (pp, name)


def init_params(options):
    """
    Global (not LSTM) parameter. For the embeding and the classifier.
    """
    params = OrderedDict()
    # embedding
    randn = numpy.random.rand(options['n_words'],
                              options['dim_proj'])
    params['Wemb'] = (0.01 * randn).astype(config.floatX)
    params = get_layer(options['encoder'])[0](options,
                                              params,
                                              prefix=options['encoder'])
    # classifier
    params['U'] = 0.01 * numpy.random.randn(options['dim_proj'],
                                            options['ydim']).astype(config.floatX)
    params['b'] = numpy.zeros((options['ydim'],)).astype(config.floatX)

    return params


def load_params(path, params):
    pp = numpy.load(path)
    for kk, vv in params.iteritems():
        if kk not in pp:
            raise Warning('%s is not in the archive' % kk)
        params[kk] = pp[kk]

    return params


def init_tparams(params):
    tparams = OrderedDict()
    for kk, pp in params.iteritems():
        tparams[kk] = theano.shared(params[kk], name=kk)
    return tparams


def get_layer(name):
    fns = layers[name]
    return fns


def ortho_weight(ndim):
    W = numpy.random.randn(ndim, ndim)
    u, s, v = numpy.linalg.svd(W)
    return u.astype(config.floatX)


def param_init_lstm(options, params, prefix='lstm'):
    """
    Init the LSTM parameter:

    :see: init_params
    """
    W = numpy.concatenate([ortho_weight(options['dim_proj']),
                           ortho_weight(options['dim_proj']),
                           ortho_weight(options['dim_proj']),
                           ortho_weight(options['dim_proj'])], axis=1)
    params[_p(prefix, 'W')] = W
    U = numpy.concatenate([ortho_weight(options['dim_proj']),
                           ortho_weight(options['dim_proj']),
                           ortho_weight(options['dim_proj']),
                           ortho_weight(options['dim_proj'])], axis=1)
    params[_p(prefix, 'U')] = U
    b = numpy.zeros((4 * options['dim_proj'],))
    params[_p(prefix, 'b')] = b.astype(config.floatX)

    return params


def lstm_layer(tparams, state_below, options, prefix='lstm', mask=None):
    nsteps = state_below.shape[0]
    if state_below.ndim == 3:
        n_samples = state_below.shape[1]
    else:
        n_samples = 1

    assert mask is not None

    def _slice(_x, n, dim):
        if _x.ndim == 3:
            return _x[:, :, n * dim:(n + 1) * dim]
        return _x[:, n * dim:(n + 1) * dim]

    def _step(m_, x_, h_, c_):
        preact = tensor.dot(h_, tparams[_p(prefix, 'U')])
        preact += x_

        i = tensor.nnet.sigmoid(_slice(preact, 0, options['dim_proj']))
        f = tensor.nnet.sigmoid(_slice(preact, 1, options['dim_proj']))
        o = tensor.nnet.sigmoid(_slice(preact, 2, options['dim_proj']))
        c = tensor.tanh(_slice(preact, 3, options['dim_proj']))


        c = f * c_ + i * c
        c = m_[:, None] * c + (1. - m_)[:, None] * c_

        h = o * tensor.tanh(c)
        h = m_[:, None] * h + (1. - m_)[:, None] * h_

        return h, c

    state_below = (tensor.dot(state_below, tparams[_p(prefix, 'W')]) +
                   tparams[_p(prefix, 'b')])


    dim_proj = options['dim_proj']
    rval, updates = theano.scan(_step,
                                sequences=[mask, state_below],
                                outputs_info=[tensor.alloc(numpy_floatX(0.),
                                                           n_samples,
                                                           dim_proj),
                                              tensor.alloc(numpy_floatX(0.),
                                                           n_samples,
                                                           dim_proj)],
                                name=_p(prefix, '_layers'),
                                n_steps=nsteps)
    return rval[0]


def param_init_gru(options, params, prefix='gru'):
    """
    Init the gru parameter:

    :see: init_params
    """
    W = numpy.concatenate([ortho_weight(options['dim_proj']),
                           ortho_weight(options['dim_proj']),
                           ortho_weight(options['dim_proj'])], axis=1)
    params[_p(prefix, 'W')] = W
    U = numpy.concatenate([ortho_weight(options['dim_proj']),
                           ortho_weight(options['dim_proj']),
                           ortho_weight(options['dim_proj'])], axis=1)
    params[_p(prefix, 'U')] = U
    b = numpy.zeros((3 * options['dim_proj'],))
    params[_p(prefix, 'b')] = b.astype(config.floatX)

    return params


def gru_layer(tparams, state_below, options, prefix='gru', mask=None):
    nsteps = state_below.shape[0]
    if state_below.ndim == 3:
        n_samples = state_below.shape[1]
    else:
        n_samples = 1

    assert mask is not None

    def _slice(_x, n, dim):
        if _x.ndim == 3:
            return _x[:, :, n * dim:(n + 1) * dim]
        return _x[:, n * dim:(n + 1) * dim]

    def _step(m_, x_, h_):
        preact = tensor.dot(h_, tparams[_p(prefix, 'U')])
        preact += x_

        i = tensor.nnet.sigmoid(_slice(preact, 0, options['dim_proj']))
        f = tensor.nnet.sigmoid(_slice(preact, 1, options['dim_proj']))
        can_h_t = tensor.tanh(_slice(preact, 2, options['dim_proj']))

        c = f * c_ + i * c
        c = m_[:, None] * c + (1. - m_)[:, None] * c_

        h = o * tensor.tanh(c)
        h = m_[:, None] * h + (1. - m_)[:, None] * h_

        return h, c

    state_below = (tensor.dot(state_below, tparams[_p(prefix, 'W')]) +
                   tparams[_p(prefix, 'b')])


    dim_proj = options['dim_proj']
    rval, updates = theano.scan(_step,
                                sequences=[mask, state_below],
                                outputs_info=[tensor.alloc(numpy_floatX(0.),
                                                           n_samples,
                                                           dim_proj),
                                              tensor.alloc(numpy_floatX(0.),
                                                           n_samples,
                                                           dim_proj)],
                                name=_p(prefix, '_layers'),
                                n_steps=nsteps)
    return rval[0]

# ff: Feed Forward (normal neural net), only useful to put after lstm
#     before the classifier.
layers = {'lstm': (param_init_lstm, lstm_layer),'gru': (param_init_gru,gru_layer)}


def sgd(lr, tparams, grads, x, mask, y, cost):
    """ Stochastic Gradient Descent

    :note: A more complicated version of sgd then needed.  This is
        done like that for adadelta and rmsprop.

    """
    # New set of shared variable that will contain the gradient
    # for a mini-batch.
    gshared = [theano.shared(p.get_value() * 0., name='%s_grad' % k)
               for k, p in tparams.iteritems()]
    gsup = [(gs, g) for gs, g in zip(gshared, grads)]

    # Function that computes gradients for a mini-batch, but do not
    # updates the weights.
    f_grad_shared = theano.function([x, mask, y], cost, updates=gsup,
                                    name='sgd_f_grad_shared')

    pup = [(p, p - lr * g) for p, g in zip(tparams.values(), gshared)]

    # Function that updates the weights from the previously computed
    # gradient.
    f_update = theano.function([lr], [], updates=pup,
                               name='sgd_f_update')

    return f_grad_shared, f_update


def adadelta(lr, tparams, grads, x, mask, y, cost):
    """
    An adaptive learning rate optimizer

    Parameters
    ----------
    lr : Theano SharedVariable
        Initial learning rate
    tpramas: Theano SharedVariable
        Model parameters
    grads: Theano variable
        Gradients of cost w.r.t to parameres
    x: Theano variable
        Model inputs
    mask: Theano variable
        Sequence mask
    y: Theano variable
        Targets
    cost: Theano variable
        Objective fucntion to minimize

    Notes
    -----
    For more information, see [ADADELTA]_.

    .. [ADADELTA] Matthew D. Zeiler, *ADADELTA: An Adaptive Learning
       Rate Method*, arXiv:1212.5701.
    """

    zipped_grads = [theano.shared(p.get_value() * numpy_floatX(0.),
                                  name='%s_grad' % k)
                    for k, p in tparams.iteritems()]
    running_up2 = [theano.shared(p.get_value() * numpy_floatX(0.),
                                 name='%s_rup2' % k)
                   for k, p in tparams.iteritems()]
    running_grads2 = [theano.shared(p.get_value() * numpy_floatX(0.),
                                    name='%s_rgrad2' % k)
                      for k, p in tparams.iteritems()]

    zgup = [(zg, g) for zg, g in zip(zipped_grads, grads)]
    rg2up = [(rg2, 0.95 * rg2 + 0.05 * (g ** 2))
             for rg2, g in zip(running_grads2, grads)]

    f_grad_shared = theano.function([x, mask, y], cost, updates=zgup + rg2up,
                                    name='adadelta_f_grad_shared')

    updir = [-tensor.sqrt(ru2 + 1e-6) / tensor.sqrt(rg2 + 1e-6) * zg
             for zg, ru2, rg2 in zip(zipped_grads,
                                     running_up2,
                                     running_grads2)]
    ru2up = [(ru2, 0.95 * ru2 + 0.05 * (ud ** 2))
             for ru2, ud in zip(running_up2, updir)]
    param_up = [(p, p + ud) for p, ud in zip(tparams.values(), updir)]

    f_update = theano.function([lr], [], updates=ru2up + param_up,
                               on_unused_input='ignore',
                               name='adadelta_f_update')

    return f_grad_shared, f_update


def rmsprop(lr, tparams, grads, x, mask, y, cost):
    """
    A variant of  SGD that scales the step size by running average of the
    recent step norms.

    Parameters
    ----------
    lr : Theano SharedVariable
        Initial learning rate
    tpramas: Theano SharedVariable
        Model parameters
    grads: Theano variable
        Gradients of cost w.r.t to parameres
    x: Theano variable
        Model inputs
    mask: Theano variable
        Sequence mask
    y: Theano variable
        Targets
    cost: Theano variable
        Objective fucntion to minimize

    Notes
    -----
    For more information, see [Hint2014]_.

    .. [Hint2014] Geoff Hinton, *Neural Networks for Machine Learning*,
       lecture 6a,
       http://cs.toronto.edu/~tijmen/csc321/slides/lecture_slides_lec6.pdf
    """

    zipped_grads = [theano.shared(p.get_value() * numpy_floatX(0.),
                                  name='%s_grad' % k)
                    for k, p in tparams.iteritems()]
    running_grads = [theano.shared(p.get_value() * numpy_floatX(0.),
                                   name='%s_rgrad' % k)
                     for k, p in tparams.iteritems()]
    running_grads2 = [theano.shared(p.get_value() * numpy_floatX(0.),
                                    name='%s_rgrad2' % k)
                      for k, p in tparams.iteritems()]

    zgup = [(zg, g) for zg, g in zip(zipped_grads, grads)]
    rgup = [(rg, 0.95 * rg + 0.05 * g) for rg, g in zip(running_grads, grads)]
    rg2up = [(rg2, 0.95 * rg2 + 0.05 * (g ** 2))
             for rg2, g in zip(running_grads2, grads)]

    f_grad_shared = theano.function([x, mask, y], cost,
                                    updates=zgup + rgup + rg2up,
                                    name='rmsprop_f_grad_shared')

    updir = [theano.shared(p.get_value() * numpy_floatX(0.),
                           name='%s_updir' % k)
             for k, p in tparams.iteritems()]
    updir_new = [(ud, 0.9 * ud - 1e-4 * zg / tensor.sqrt(rg2 - rg ** 2 + 1e-4))
                 for ud, zg, rg, rg2 in zip(updir, zipped_grads, running_grads,
                                            running_grads2)]
    param_up = [(p, p + udn[1])
                for p, udn in zip(tparams.values(), updir_new)]
    f_update = theano.function([lr], [], updates=updir_new + param_up,
                               on_unused_input='ignore',
                               name='rmsprop_f_update')

    return f_grad_shared, f_update


def build_model(tparams, options):
    trng = RandomStreams(SEED)

    # Used for dropout.
    use_noise = theano.shared(numpy_floatX(0.))

    x = tensor.matrix('x', dtype='int64')
    mask = tensor.matrix('mask', dtype=config.floatX)
    y = tensor.vector('y', dtype='int64')

    n_timesteps = x.shape[0]
    n_samples = x.shape[1]

    emb = tparams['Wemb'][x.flatten()].reshape([n_timesteps,
                                                n_samples,
                                                options['dim_proj']])
    proj = get_layer(options['encoder'])[1](tparams, emb, options,
                                            prefix=options['encoder'],
                                            mask=mask)
    if options['encoder'] == 'lstm':
        proj = (proj * mask[:, :, None]).sum(axis=0)
        proj = proj / mask.sum(axis=0)[:, None]
    if options['use_dropout']:
        proj = dropout_layer(proj, use_noise, trng)

    pred = tensor.nnet.softmax(tensor.dot(proj, tparams['U']) + tparams['b'])

    f_pred_prob = theano.function([x, mask], pred, name='f_pred_prob')
    f_pred = theano.function([x, mask], pred.argmax(axis=1), name='f_pred')

    off = 1e-8
    if pred.dtype == 'float16':
        off = 1e-6

    cost = -tensor.log(pred[tensor.arange(n_samples), y] + off).mean()

    return use_noise, x, mask, y, f_pred_prob, f_pred, cost


def pred_probs(f_pred_prob, prepare_data, data, iterator, categories, verbose=False):
    """ If you want to use a trained model, this is useful to compute
    the probabilities of new examples.
    """
    n_samples = len(data[0])
    probs = numpy.zeros((n_samples, categories)).astype(config.floatX)

    n_done = 0

    for _, valid_index in iterator:
        x, mask, y = prepare_data([data[0][t] for t in valid_index],
                                  numpy.array(data[1])[valid_index],
                                  maxlen=None)
        pred_probs = f_pred_prob(x, mask)


        probs[valid_index,:] = pred_probs

        n_done += len(valid_index)
        if verbose:
            print '%d/%d samples classified' % (n_done, n_samples)

    return probs

def pred(f_pred, prepare_data, data, iterator, verbose=False):
    """
    Just compute the predictions
    f_pred: Theano fct computing the prediction
    prepare_data: usual prepare_data for that dataset.
    """
    n_samples = len(data[0])
    preds = numpy.zeros((n_samples)).astype(config.floatX)

    n_done = 0

    for _, valid_index in iterator:
        x, mask, y = prepare_data([data[0][t] for t in valid_index],
                                  numpy.array(data[1])[valid_index],
                                  maxlen=None)
        prediction = f_pred(x, mask)
        preds[valid_index] = prediction

        n_done += len(valid_index)
        if verbose:
            print '%d/%d samples classified' % (n_done, n_samples)

    return preds

def pred_error(f_pred, prepare_data, data, iterator, verbose=False):
    """
    Just compute the error
    f_pred: Theano fct computing the prediction
    prepare_data: usual prepare_data for that dataset.
    """
    valid_err = 0
    for _, valid_index in iterator:
        x, mask, y = prepare_data([data[0][t] for t in valid_index],
                                  numpy.array(data[1])[valid_index],
                                  maxlen=None)
        preds = f_pred(x, mask)
        targets = numpy.array(data[1])[valid_index]
        valid_err += (preds == targets).sum()
    valid_err = 1. - numpy_floatX(valid_err) / len(data[0])

    return valid_err

def final_errors(predictions, target, verbose=False):
    """
    Just compute the error
    f_pred: Theano fct computing the prediction
    prepare_data: usual prepare_data for that dataset.
    """
    all_error = 0
    one_cat_error = 0
    two_cat_error = 0

    total_data = len(predictions[0])

    #data = numpy.dstack(predictions, target)
    
    errors = numpy.sum(numpy.equal(predictions, target),axis = 0)
    all_errors = numpy.sum(errors == 3)
    two_cat_errors = numpy.sum(errors == 2)
    one_cat_errors = numpy.sum(errors == 1)
    all_error = 1. - numpy_floatX(all_errors) / total_data
    two_cat_error = 1. - numpy_floatX(two_cat_errors) / total_data
    one_cat_error = 1. - numpy_floatX(one_cat_errors) / total_data


    return all_error, two_cat_error, one_cat_error

def get_data(
    n_words=50000,  # Vocabulary size
    maxlen=100,  # Sequence longer than this get ignored
    dataset='nordstrom',
    path = 'data/descriptions/', #This is the path for the dictionaries to use
    test_size=-1,  # If >0, we keep only this number of test example.
    train_size = 2500, # If >0, we keep only this number of train example.
):

    load_data, prepare_data = get_dataset(dataset)

    print 'Loading data'
    train, valid, test, dictionary = nordstrom.load_data(path = folder_path + path + 'nordstrom', n_words=n_words, valid_portion=0.1,
                                   maxlen=maxlen)

    if test_size > 0:
        # The test set is sorted by size, but we want to keep random
        # size example.  So we must select a random selection of the
        # examples.
        idx = numpy.arange(len(test[0]))
        #numpy.random.seed(1555)
        numpy.random.shuffle(idx)

        idx = idx[:test_size]
        test = ([test[0][n] for n in idx], [test[1][n] for n in idx],
                [test[2][n] for n in idx], [test[3][n] for n in idx],
                [test[4][n] for n in idx], [test[5][n] for n in idx])

    if train_size > 0:
        # The test set is sorted by size, but we want to keep random
        # size example.  So we must select a random selection of the
        # examples.
        idx = numpy.arange(len(train[0]))
        numpy.random.shuffle(idx)
        idx = idx[:train_size]
        train = ([train[0][n] for n in idx], [train[1][n] for n in idx],
                [train[2][n] for n in idx],[train[3][n] for n in idx],
                [train[4][n] for n in idx],[train[5][n] for n in idx])

        idx = numpy.arange(len(valid[0]))
        numpy.random.shuffle(idx)
        idx = idx[:train_size*0.1]
        valid = ([valid[0][n] for n in idx], [valid[1][n] for n in idx],
                [valid[2][n] for n in idx], [valid[3][n] for n in idx],
                [valid[4][n] for n in idx], [valid[5][n] for n in idx])

    data = (train, valid, test, dictionary)
    return data

def get_features(data=None, predictions=None, cat=1):

    train, valid, test, dictionary = data
    train = copy.deepcopy(train)
    valid = copy.deepcopy(valid)
    test = copy.deepcopy(test)
    dictionary, cat_1, cat_2 = dictionary

    col = cat + 2

    if cat > 1:
        count = 0
        for ss, prev_cat in zip(train[0], train[col]):
            #print prev_cat
            ss.append(prev_cat)
            train[0][count] = ss
            count += 1
        count = 0
        for ss, prev_cat in zip(valid[0], valid[col]):
            #print prev_cat
            ss.append(prev_cat)
            valid[0][count] = ss
            count += 1
            #print ss
        count = 0
        for ss, prev_pred in zip(test[0],predictions):
            if cat == 2:
                #print dictionary[cat_1[prev_pred]]
                name = cat_1[prev_pred]
                ss.append(dictionary[name])
                test[0][count] = ss
            if cat == 3:
                #print dictionary[cat_2[prev_pred]]
                name = cat_2[prev_pred]
                ss.append(dictionary[name])
                test[0][count] = ss
            count += 1            

    return train, valid, test


def train_lstm(
    data = None,
    dim_proj=128,  # word embeding dimension and LSTM number of hidden units.
    patience=10,  # Number of epoch to wait before early stop if no progress
    max_epochs=5000,  # The maximum number of epoch to run
    dispFreq=10,  # Display to stdout the training progress every N updates
    decay_c=0.,  # Weight decay for the classifier applied to the U weights.
    lrate=0.0001,  # Learning rate for sgd (not used for adadelta and rmsprop)
    n_words=50000,  # Vocabulary size
    optimizer=adadelta,  # sgd, adadelta and rmsprop available, sgd very hard to use, not recommanded (probably need momentum and decaying learning rate).
    encoder='lstm',  # TODO: can be removed must be lstm.
    saveto='nordstrom_model.npz',  # The best model will be saved there
    validFreq=370,  # Compute the validation error after this number of update.
    saveFreq=1110,  # Save the parameters after every saveFreq updates
    maxlen=100,  # Sequence longer then this get ignored
    batch_size=16,  # The batch size during training.
    valid_batch_size=64,  # The batch size used for validation/test set.
    dataset='nordstrom',
    path = 'data/descriptions/', #This is the path for the dictionaries to use

    # Parameter for extra option
    noise_std=0.,
    use_dropout=True,  # if False slightly faster, but worst test error
                       # This frequently need a bigger model.
    reload_model=None,  # Path to a saved model we want to start from.
    cat_level = 1, #the level of category to predict
    predictions = None, #the predictions from the previous category run
):

    # Model options
    model_options = locals().copy()
    model_options['data'] = None
    print "model options", model_options

    load_data, prepare_data = get_dataset(dataset)
    
    train, valid, test = data

    ydim = numpy.max(train[2+cat_level]) + 1

    train = (train[0],train[2+cat_level])
    valid = (valid[0],valid[2+cat_level])
    test = (test[0],test[2+cat_level])

    data = (train, valid, test)

    model_options['data'] = data
    model_options['ydim'] = ydim

    print 'Building model'
    # This create the initial parameters as numpy ndarrays.
    # Dict name (string) -> numpy ndarray
    params = init_params(model_options)

    if reload_model:
        load_params(reload_model, params)

    # This create Theano Shared Variable from the parameters.
    # Dict name (string) -> Theano Tensor Shared Variable
    # params and tparams have different copy of the weights.
    tparams = init_tparams(params)

    # use_noise is for dropout
    (use_noise, x, mask,
     y, f_pred_prob, f_pred, cost) = build_model(tparams, model_options)

    if decay_c > 0.:
        decay_c = theano.shared(numpy_floatX(decay_c), name='decay_c')
        weight_decay = 0.
        weight_decay += (tparams['U'] ** 2).sum()
        weight_decay *= decay_c
        cost += weight_decay

    f_cost = theano.function([x, mask, y], cost, name='f_cost')

    grads = tensor.grad(cost, wrt=tparams.values())
    f_grad = theano.function([x, mask, y], grads, name='f_grad')

    lr = tensor.scalar(name='lr')
    f_grad_shared, f_update = optimizer(lr, tparams, grads,
                                        x, mask, y, cost)

    print 'Optimization'

    kf_valid = get_minibatches_idx(len(valid[0]), valid_batch_size)
    kf_test = get_minibatches_idx(len(test[0]), valid_batch_size)

    print "%d train examples" % len(train[0])
    print "%d valid examples" % len(valid[0])
    print "%d test examples" % len(test[0])

    history_errs = []
    best_p = None
    bad_count = 0

    if validFreq == -1:
        validFreq = len(train[0]) / batch_size
    if saveFreq == -1:
        saveFreq = len(train[0]) / batch_size

    uidx = 0  # the number of update done
    estop = False  # early stop
    start_time = time.time()
    try:
        for eidx in xrange(max_epochs):
            n_samples = 0

            # Get new shuffled index for the training set.
            kf = get_minibatches_idx(len(train[0]), batch_size, shuffle=True)

            for _, train_index in kf:
                uidx += 1
                use_noise.set_value(1.)

                # Select the random examples for this minibatch
                y = [train[1][t] for t in train_index]
                x = [train[0][t]for t in train_index]

                # Get the data in numpy.ndarray format
                # This swap the axis!
                # Return something of shape (minibatch maxlen, n samples)
                x, mask, y = prepare_data(x, y)
                n_samples += x.shape[1]

                cost = f_grad_shared(x, mask, y)
                f_update(lrate)

                if numpy.isnan(cost) or numpy.isinf(cost):
                    print 'NaN detected'
                    return 1., 1., 1.

                if numpy.mod(uidx, dispFreq) == 0:
                    print 'Epoch ', eidx, 'Update ', uidx, 'Cost ', cost

                if saveto and numpy.mod(uidx, saveFreq) == 0:
                    print 'Saving...',

                    if best_p is not None:
                        params = best_p
                    else:
                        params = unzip(tparams)
                    numpy.savez(saveto, history_errs=history_errs, **params)
                    pkl.dump(model_options, open('%s.pkl' % saveto, 'wb'), -1)
                    print 'Done'

                if numpy.mod(uidx, validFreq) == 0:
                    use_noise.set_value(0.)
                    train_err = pred_error(f_pred, prepare_data, train, kf)
                    valid_err = pred_error(f_pred, prepare_data, valid,
                                           kf_valid)
                    test_err = pred_error(f_pred, prepare_data, test, kf_test)

                    history_errs.append([valid_err, test_err])

                    if (uidx == 0 or
                        valid_err <= numpy.array(history_errs)[:,
                                                               0].min()):

                        best_p = unzip(tparams)
                        bad_counter = 0

                    print ('Train ', train_err, 'Valid ', valid_err,
                           'Test ', test_err)

                    if (len(history_errs) > patience and
                        valid_err >= numpy.array(history_errs)[:-patience,
                                                               0].min()):
                        bad_counter += 1
                        if bad_counter > patience:
                            print 'Early Stop!'
                            estop = True
                            break

            print 'Seen %d samples' % n_samples

            if estop:
                break

    except KeyboardInterrupt:
        print "Training interupted"

    end_time = time.time()
    if best_p is not None:
        zipp(best_p, tparams)
    else:
        best_p = unzip(tparams)

    use_noise.set_value(0.)
    kf_train_sorted = get_minibatches_idx(len(train[0]), batch_size)
    train_err = pred_error(f_pred, prepare_data, train, kf_train_sorted)
    valid_err = pred_error(f_pred, prepare_data, valid, kf_valid)
    test_err = pred_error(f_pred, prepare_data, test, kf_test)

    predictions = pred(f_pred, prepare_data, test, kf_test)
    prediction_probs = pred_probs(f_pred_prob, prepare_data, test, kf_test, ydim)
    prediction_probs = numpy.max(prediction_probs,axis = 1)

    print 'Train ', train_err, 'Valid ', valid_err, 'Test ', test_err
    if saveto:
        numpy.savez(saveto, train_err=train_err,
                    valid_err=valid_err, test_err=test_err,
                    history_errs=history_errs, predictions = predictions,
                    pred_probs = prediction_probs, **best_p)
    print 'The code run for %d epochs, with %f sec/epochs' % (
        (eidx + 1), (end_time - start_time) / (1. * (eidx + 1)))
    print >> sys.stderr, ('Training took %.1fs' %
                          (end_time - start_time))
    return model_options, predictions, prediction_probs

def get_top_probs(prediction_probs, pred_size):
    predictions = numpy.argsort(-prediction_probs)
    probs = -numpy.sort(-prediction_probs)
    top_preds = predictions[:,:pred_size].T
    top_probs = probs[:,:pred_size].T

    return top_preds, top_probs

def pred_multiple(data = None, model_options_1 = None, 
    model_options_2 = None, model_options_3 = None,
    pred_size = 5, reload_model_1 = None, reload_model_2 = None, 
    reload_model_3 = None):

    #predictions = numpy.zeros((len(prev_predictions)*pred_size,len(data))).astype(config.floatX)
    #probabilities = numpy.zeros((len(prev_predictions)*pred_size,len(data))).astype(config.floatX)
    
    #set up the model
    load_data, prepare_data = get_dataset(model_options_1['dataset'])
    params_mod_1 = init_params(model_options_1)
    load_params(reload_model_1, params_mod_1)
    params_mod_2 = init_params(model_options_2)
    load_params(reload_model_2,params_mod_2)
    params_mod_3 = init_params(model_options_3)
    load_params(reload_model_3, params_mod_3)

    tparams_1 = init_tparams(params_mod_1)
    tparams_2 = init_tparams(params_mod_2)
    tparams_3 = init_tparams(params_mod_3)

    train,valid,test,dictionary = data
    test = copy.deepcopy(test)
    dictionary, cat_1, cat_2 = dictionary

    ydim = numpy.max(train[3]) + 1
    kf = get_minibatches_idx(len(test[0]),model_options_1['valid_batch_size'])

    new_test = (test[0],test[3])
    
    #predict cat_1
    (use_noise, x, mask,
    y, f_pred_prob, f_pred, cost) = build_model(tparams_1, model_options_1)
    prediction_probs = pred_probs(f_pred_prob, prepare_data, test, kf, ydim)
    top_preds_1, top_probs_1 = get_top_probs(prediction_probs, pred_size)

    #predict cat_2
    ydim = numpy.max(train[4]) + 1
    (use_noise, x, mask,
    y, f_pred_prob, f_pred, cost) = build_model(tparams_2, model_options_2)
    new_test = (test[0],test[4])

    probabilities = numpy.zeros((len(new_test[0]),pred_size,ydim))
    for idx in range(len(top_preds_1)):
        preds = top_preds_1[idx]
        count = 0
        for ss, prev_pred in zip(new_test[0],preds):
            name = cat_1[prev_pred]
            ss.append(dictionary[name])
            new_test[0][count] = ss
            count += 1   

        prediction_probs = pred_probs(f_pred_prob, prepare_data, new_test, kf, ydim)

        probabilities[:,idx,:] = prediction_probs

    probabilities = probabilities.reshape(len(new_test[0]), ydim * pred_size)
    _top_pred_per_prev_probs = numpy.argsort(-probabilities)
    top_preds_1 = (_top_pred_per_prev_probs / ydim)[:,:pred_size]
    top_preds_2 = (_top_pred_per_prev_probs % ydim)[:,:pred_size]

    #top_preds_all_cats = numpy.dstack(top_preds_1, top_preds_2)

    #predict cat_3
    ydim = numpy.max(train[5]) + 1
    (use_noise, x, mask,
    y, f_pred_prob, f_pred, cost) = build_model(tparams_3, model_options_3)
    new_test = (test[0],test[5])

    probabilities = numpy.zeros((len(new_test[0]),pred_size,ydim))
    for idx in range(len(top_preds_2.T)):
        preds = top_preds_2.T[idx]
        count = 0
        for ss, prev_pred in zip(new_test[0],preds):
            name = cat_2[prev_pred]
            ss.append(dictionary[name])
            new_test[0][count] = ss
            count += 1   

        prediction_probs = pred_probs(f_pred_prob, prepare_data, new_test, kf, ydim)

        probabilities[:,idx,:] = prediction_probs


    probabilities = probabilities.reshape(len(new_test[0]), ydim * pred_size)
    _top_pred_per_prev_probs = numpy.argsort(-probabilities)
    top_prev_idx = (_top_pred_per_prev_probs / ydim)[:,:1]
    top_preds_3 = (_top_pred_per_prev_probs % ydim)[:,:1]

    #get the final predictions for all 3 categories
    final_predictions = numpy.zeros((3,len(new_test[0])))
    for idx, pred, prev_idx in zip(numpy.arange(len(new_test[0])),top_preds_3,top_prev_idx):
        final_predictions[:,idx] = top_preds_1[idx][prev_idx],top_preds_2[idx][prev_idx],pred
    

    final_probabilities = numpy.max(probabilities, axis = 1)


    return final_predictions,probabilities      

def main():

    data = get_data(test_size=5000, 
        train_size = 50000,
        dataset='nordstrom',
        path = 'data/encode_cats/')

    save_path = '../results/'
    save_folder = 'pred_probs/'
    
    train, valid, test, dictionary = data

    data_1 = get_features(data = data)

    max_epochs = 1
    dataset = 'nordstrom'
    path = 'data/encode_cats/'
    saveto=save_path + save_folder

    pred_size = 5

    model_options_1, predictions_1, prediction_probs_1 = train_lstm(
        data = data_1,
        dim_proj=128,
        max_epochs=max_epochs,
        cat_level = 1,
        dataset='nordstrom',
        path = 'data/encode_cats/',
        saveto=saveto + 'nordstrom_model_cat_1.npz',
        reload_model=None
    )

    data_2 = get_features(data=data, predictions=predictions_1, cat=2)

    model_options_2, predictions_2, prediction_probs_2 = train_lstm(
        data = data_2,
        max_epochs=max_epochs,
        cat_level = 2,
        dataset='nordstrom',
        path = 'data/encode_cats/',
        saveto=save_path + save_folder + 'nordstrom_model_cat_2.npz',
        reload_model=None
    )

    data_3 = get_features(data=data, predictions=predictions_2, cat=3)

    model_options_3, predictions_3, prediction_probs_3 = train_lstm(
        data = data_3,
        max_epochs=max_epochs,
        cat_level = 3,
        dataset='nordstrom',
        path = 'data/encode_cats/',
        saveto=save_path + save_folder + 'nordstrom_model_cat_3.npz',
        reload_model=None
    )

    hard_predictions = numpy.vstack((predictions_1, predictions_2,predictions_3))
    hard_probs = numpy.vstack((prediction_probs_1, prediction_probs_2,prediction_probs_3))

    soft_predictions, soft_probabilities = pred_multiple(data = data, 
        model_options_1 = model_options_1,
        model_options_2 = model_options_2,
        model_options_3 = model_options_3, 
        pred_size = pred_size, 
        reload_model_1 = save_path + save_folder + 'nordstrom_model_cat_1.npz', 
        reload_model_2 = save_path + save_folder + 'nordstrom_model_cat_2.npz',
        reload_model_3 = save_path + save_folder + 'nordstrom_model_cat_3.npz'
    )


    target = numpy.vstack((test[3],test[4],test[5]))

    numpy.savez(save_path + save_folder + 'nordstrom_model_lstm_encode.npz', 
        hard_predictions = hard_predictions, 
        hard_prediction_probs = hard_probs,
        soft_pred = soft_predictions, 
        soft_probs = soft_probabilities, 
        target = target
    )

    hard_all_err, hard_two_cat_err, hard_one_cat_err = final_errors(hard_predictions, target, verbose=False)
    soft_all_err, soft_two_cat_err, soft_one_cat_err = final_errors(soft_predictions, target, verbose=False)

    print 'hard -  all_errors: %s | two_cat_errors : %s | one_cat_errors: %s' %(hard_all_err, hard_two_cat_err, hard_one_cat_err)
    print 'soft - all_errors: %s | two_cat_errors : %s | one_cat_errors: %s' %(soft_all_err, soft_two_cat_err, soft_one_cat_err)

    numpy.savez(saveto, train_err_1=train_err_1,
                   valid_err_1=valid_err_1, test_err_1=test_err_1,
                    history_errs=history_errs, **best_p)
if __name__ == '__main__':
    # See function train for all possible parameter and there definition.
    main()

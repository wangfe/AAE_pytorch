import argparse
import time
import torch
import pickle
import numpy as np
import itertools
from torch.autograd import Variable
import torch.nn as nn
import torch.nn.functional as F
import torch.autograd as autograd
import torch.optim as optim
from torch.nn.modules.upsampling import UpsamplingNearest2d

# Training settings
parser = argparse.ArgumentParser(description='PyTorch semi-supervised MNIST')

parser.add_argument('--batch-size', type=int, default=100, metavar='N',
                    help='input batch size for training (default: 100)')
parser.add_argument('--epochs', type=int, default=5000, metavar='N',
                    help='number of epochs to train (default: 10)')
parser.add_argument('--no-cuda', action='store_true', default=False,
                    help='enables CUDA training')


args = parser.parse_args()
args.cuda = not args.no_cuda and torch.cuda.is_available()

seed = 10
if args.cuda:
    torch.cuda.manual_seed(seed)
cuda = args.cuda

kwargs = {'num_workers': 1, 'pin_memory': True} if cuda else {}
n_classes = 10
z_dim = 10
X_dim = 784
y_dim = 10
convolutional = False
train_batch_size = args.batch_size
valid_batch_size = args.batch_size
N = 1000
epochs = args.epochs

##################################
# Load data and create Data loaders
##################################
def load_data(data_path='../data/'):
    print('loading data!')
    trainset_labeled = pickle.load(open(data_path + "train_labeled.p", "rb"))
    trainset_unlabeled = pickle.load(open(data_path + "train_unlabeled.p", "rb"))
    # Set -1 as labels for unlabeled data
    trainset_unlabeled.train_labels = torch.from_numpy(np.array([-1] * 47000))
    validset = pickle.load(open(data_path + "validation.p", "rb"))

    train_labeled_loader = torch.utils.data.DataLoader(trainset_labeled,
                                                       batch_size=train_batch_size,
                                                       shuffle=True, **kwargs)

    train_unlabeled_loader = torch.utils.data.DataLoader(trainset_unlabeled,
                                                         batch_size=train_batch_size,
                                                         shuffle=True, **kwargs)

    valid_loader = torch.utils.data.DataLoader(validset, batch_size=valid_batch_size, shuffle=True)

    return train_labeled_loader, train_unlabeled_loader, valid_loader

##################################
# Define Networks
##################################
# Encoder
class Q_net(nn.Module):
    def __init__(self):
        super(Q_net, self).__init__()
        self.lin1 = nn.Linear(X_dim, N)
        self.lin2 = nn.Linear(N, N)
        # Categorical code (y)
        self.lin3cat = nn.Linear(N, n_classes)
        # Gaussian code (z)
        self.lin3gauss = nn.Linear(N, z_dim)

    def forward(self, x):
        x = F.droppout(self.lin1(x), p=0.25, training=self.training)
        x = F.relu(x)
        x = F.droppout(self.lin2(x), p=0.25, training=self.training)
        x = F.relu(x)
        xcat = F.softmax(self.lin3cat(x))
        xgauss = self.lin3gauss(x)

        return xcat, xgauss

# Decoder
class P_net(nn.Module):
    def __init__(self):
        super(P_net, self).__init__()
        self.lin1 = nn.Linear(z_dim + n_classes, N)
        self.lin2 = nn.Linear(N, N)
        self.lin3 = nn.Linear(N, X_dim)

    def forward(self, x):
        x = self.lin1(x)
        x = F.dropout(x, p=0.25, training=self.training)
        x = F.relu(x)
        x = self.lin2(x)
        x = F.dropout(x, p=0.25, training=self.training)
        x = self.lin3(x)
        return F.sigmoid(x)


# Discriminator networks
class D_net_cat(nn.Module):
    def __init__(self):
        super(D_net_cat, self).__init__()
        self.lin1 = nn.Linear(n_classes, N)
        self.lin2 = nn.Linear(N, N)
        self.lin3 = nn.Linear(N, 1)

    def forward(self, x):
        x = F.dropout(self.lin1(x), p=0.2, training=self.training)
        x = F.relu(x)
        F.dropout(self.lin2(x), p=0.2, training=self.training)
        x = F.relu(x)
        return F.sigmoid(self.lin3(x))


class D_net_gauss(nn.Module):
    def __init__(self):
        super(D_net_gauss, self).__init__()
        self.lin1 = nn.Linear(z_dim, N)
        self.lin2 = nn.Linear(N, N)
        self.lin3 = nn.Linear(N, 1)

    def forward(self, x):
        x = F.dropout(self.lin1(x), p=0.2, training=self.training)
        x = F.relu(x)
        x = F.dropout(self.lin2(x), p=0.2, training=self.training)
        x = F.relu(x)

        return F.sigmoid(self.lin3(x))


####################
# Utility functions
####################
def save_model(model, filename):
    print('Best model so far, saving it...')
    torch.save(model.state_dict(), filename)

def sample_categorical(batch_size, n_classes=10):
    '''
     Sample from a categorical distribution
     of size batch_size and # of classes n_classes
     return: torch.autograd.Variable with the sample
    '''
    cat = np.random.randint(0, 10, batch_size)
    cat = np.eye(n_classes)[cat].astype('float32')
    cat = torch.from_numpy(cat)
    return Variable(cat)


def report_loss(epoch, D_loss_cat, D_loss_gauss, G_loss, recon_loss):
    '''
    Print loss
    '''
    print('Epoch-{}; D_loss_cat: {:.4}: D_loss_gauss: {:.4}; G_loss: {:.4}; recon_loss: {:.4}'.format(epoch,
                                                                                                      D_loss_cat.data[0],
                                                                                                      D_loss_gauss.data[0],
                                                                                                      G_loss.data[0],
                                                                                                      recon_loss.data[0]))


def create_latent(Q, loader):
    '''
    Creates the latent representation for the samples in loader
    return:
        z_values: numpy array with the latent representations
        labels: the labels corresponding to the latent representations
    '''
    Q.eval()
    labels = []

    for batch_idx, (X, target) in enumerate(loader):

        X = X * 0.3081 + 0.1307
        # X.resize_(loader.batch_size, X_dim)
        X, target = Variable(X), Variable(target)
        labels.extend(target.data.tolist())
        if cuda:
            X, target = X.cuda(), target.cuda()
        # Reconstruction phase
        z_sample = Q(X)
        if batch_idx > 0:
            z_values = np.concatenate((z_values, np.array(z_sample.data.tolist())))
        else:
            z_values = np.array(z_sample.data.tolist())
    labels = np.array(labels)

    return z_values, labels


def classification_accuracy(Q, data_loader):
    Q.eval()
    labels = []
    test_loss = 0
    correct = 0

    for batch_idx, (X, target) in enumerate(data_loader):
        X = X * 0.3081 + 0.1307
        X.resize_(data_loader.batch_size, X_dim)
        X, target = Variable(X), Variable(target)
        if cuda:
            X, target = X.cuda(), target.cuda()

        labels.extend(target.data.tolist())
        # Reconstruction phase
        output = Q(X)[0]

        test_loss += F.nll_loss(output, target).data[0]

        pred = output.data.max(1)[1]
        correct += pred.eq(target.data).cpu().sum()

    test_loss /= len(data_loader)
    return 100. * correct / len(data_loader.dataset)

def newdataset():
    z_cat = np.arange(0, n_classes)
    z_cat = np.eye(n_classes)[z_cat].astype('float32')
    z_cat = torch.from_numpy(z_cat)
    z_cat = Variable(z_cat)
    if cuda:
        z_cat = z_cat.cuda()
    
    z_real_gauss = Variable(torch.randn(10, z_dim))
    if cuda:
        z_real_gauss = z_real_gauss.cuda()
    X_sample = P(torch.cat((z_cat, z_real_gauss),1))

    for i in range(5000):
        z_real_gauss = Variable(torch.randn(10, z_dim))
        if cuda:
            z_real_gauss = z_real_gauss.cuda()
        x_sample = P(torch.cat((z_cat, z_real_gauss),1))
        X_sample = torch.cat((x_sample, X_sample),0)

    xs = np.array(X_sample.data.tolist())

    pkl.dump(xs, open('./synthetic_samples', 'w'))

####################
# Train procedure
####################
def train(P, Q, D_cat, D_gauss, P_decoder, Q_encoder, Q_semi_supervised, Q_generator, D_cat_solver, D_gauss_solver, train_labeled_loader, train_unlabeled_loader):
    '''
    Train procedure for one epoch.
    '''
    TINY = 1e-15
    # Set the networks in train mode (apply dropout when needed)
    Q.train()
    P.train()
    D_cat.train()
    D_gauss.train()

    if train_unlabeled_loader is None:
        train_unlabeled_loader = train_labeled_loader

    # Loop through the labeled and unlabeled dataset getting one batch of samples from each
    # The batch size has to be a divisor of the size of the dataset or it will return
    # invalid samples
    for (X_l, target_l), (X_u, target_u) in itertools.izip(train_labeled_loader, train_unlabeled_loader):

        for X, target in [(X_u, target_u), (X_l, target_l)]:
            if target[0] == -1:
                labeled = False
            else:
                labeled = True

            # Load batch and normalize samples to be between 0 and 1
            X = X * 0.3081 + 0.1307
            X.resize_(train_batch_size, X_dim)

            X, target = Variable(X), Variable(target)
            if cuda:
                X, target = X.cuda(), target.cuda()

            # Init gradients
            P.zero_grad()
            Q.zero_grad()
            D_cat.zero_grad()
            D_gauss.zero_grad()

            #######################
            # Reconstruction phase
            #######################
            if not labeled:
                z_sample = torch.cat(Q(X), 1)
                X_sample = P(z_sample)

                recon_loss = F.binary_cross_entropy(X_sample + TINY, X.resize(train_batch_size, X_dim) + TINY)
                recon_loss = recon_loss
                recon_loss.backward()
                P_decoder.step()
                Q_encoder.step()

                P.zero_grad()
                Q.zero_grad()
                D_cat.zero_grad()
                D_gauss.zero_grad()
                recon_loss = recon_loss
                #######################
                # Regularization phase
                #######################
                # Discriminator
                Q.eval()
                z_real_cat = sample_categorical(train_batch_size, n_classes=n_classes)
                z_real_gauss = Variable(torch.randn(train_batch_size, z_dim))
                if cuda:
                    z_real_cat = z_real_cat.cuda()
                    z_real_gauss = z_real_gauss.cuda()

                z_fake_cat, z_fake_gauss = Q(X)

                D_real_cat = D_cat(z_real_cat)
                D_real_gauss = D_gauss(z_real_gauss)
                D_fake_cat = D_cat(z_fake_cat)
                D_fake_gauss = D_gauss(z_fake_gauss)

                D_loss_cat = -torch.mean(torch.log(D_real_cat + TINY) + torch.log(1 - D_fake_cat + TINY))
                D_loss_gauss = -torch.mean(torch.log(D_real_gauss + TINY) + torch.log(1 - D_fake_gauss + TINY))

                D_loss = D_loss_cat + D_loss_gauss
                D_loss = D_loss

                D_loss.backward()
                D_cat_solver.step()
                D_gauss_solver.step()

                P.zero_grad()
                Q.zero_grad()
                D_cat.zero_grad()
                D_gauss.zero_grad()

                # Generator
                Q.train()
                z_fake_cat, z_fake_gauss = Q(X)

                D_fake_cat = D_cat(z_fake_cat)
                D_fake_gauss = D_gauss(z_fake_gauss)

                G_loss = - torch.mean(torch.log(D_fake_cat + TINY)) - torch.mean(torch.log(D_fake_gauss + TINY))
                G_loss = G_loss
                G_loss.backward()
                Q_generator.step()

                P.zero_grad()
                Q.zero_grad()
                D_cat.zero_grad()
                D_gauss.zero_grad()

            #######################
            # Semi-supervised phase
            #######################
            if labeled:
                pred, _ = Q(X)
                class_loss = F.cross_entropy(pred, target)
                class_loss.backward()
                Q_semi_supervised.step()

                P.zero_grad()
                Q.zero_grad()
                D_cat.zero_grad()
                D_gauss.zero_grad()

    return D_loss_cat, D_loss_gauss, G_loss, recon_loss, class_loss


def generate_model(savefolder='./'):
    train_labeled_loader, train_unlabeled_loader, valid_loader = load_data()

    # Create and initialize Networks
    train_acc = []
    val_acc = []

    torch.manual_seed(10)

    if cuda:
        Q = Q_net().cuda()
        P = P_net().cuda()
        D_cat = D_net_cat().cuda()
        D_gauss = D_net_gauss().cuda()
    else:
        Q = Q_net()
        P = P_net()
        D_gauss = D_net_gauss()
        D_cat = D_net_cat()

    # Set learning rates
    gen_lr = 0.0006
    semi_lr = 0.001
    reg_lr = 0.0008

    # Set optimizators
    P_decoder = optim.Adam(P.parameters(), lr=gen_lr)
    Q_encoder = optim.Adam(Q.parameters(), lr=gen_lr)

    Q_semi_supervised = optim.Adam(Q.parameters(), lr=semi_lr)

    Q_generator = optim.Adam(Q.parameters(), lr=reg_lr)
    D_gauss_solver = optim.Adam(D_gauss.parameters(), lr=reg_lr)
    D_cat_solver = optim.Adam(D_cat.parameters(), lr=reg_lr)

    for epoch in range(epochs):
        if epoch == 75:
            P_decoder = optim.Adam(P.parameters(), lr=gen_lr / 10.)
            Q_encoder = optim.Adam(Q.parameters(), lr=gen_lr / 10.)

            Q_semi_supervised = optim.Adam(Q.parameters(), lr=semi_lr / 10.)

            Q_generator = optim.Adam(Q.parameters(), lr=reg_lr / 10.)
            D_gauss_solver = optim.Adam(D_gauss.parameters(), lr=reg_lr / 10.)
            D_cat_solver = optim.Adam(D_cat.parameters(), lr=reg_lr / 10.)

        D_loss_cat, D_loss_gauss, G_loss, recon_loss, class_loss = train(P, Q, D_cat,
                                                                         D_gauss, P_decoder,
                                                                         Q_encoder, Q_semi_supervised,
                                                                         Q_generator,
                                                                         D_cat_solver, D_gauss_solver,
                                                                         train_labeled_loader,
                                                                         train_unlabeled_loader)
        if epoch % 10 == 0:
            train_acc.append(classification_accuracy(Q, train_labeled_loader))
            val_acc.append(classification_accuracy(Q, valid_loader))
            report_loss(epoch, D_loss_cat, D_loss_gauss, G_loss, recon_loss)
            print('Classification Loss: {:.3}'.format(class_loss.data[0]))
            print('Validation accuracy: {} %'.format(val_acc[-1]))

    torch.save(Q.state_dict(), savefolder + 'Q_networkL')
    torch.save(P.state_dict(), savefolder + 'P_network')
    torch.save(D_cat.state_dict(), savefolder + 'D_cat_network')
    torch.save(D_gauss.state_dict(), savefolder + 'D_gauss_network')


if __name__ == '__main__':
    generate_model()







import argparse
import time
import torch
import pickle
import numpy as np
import itertools
from torch.autograd import Variable
import torch.nn as nn
import torch.nn.functional as F
import torch.autograd as autograd
import torch.optim as optim
from torch.nn.modules.upsampling import UpsamplingNearest2d

# Training settings
parser = argparse.ArgumentParser(description='PyTorch semi-supervised MNIST')

parser.add_argument('--batch-size', type=int, default=100, metavar='N',
                    help='input batch size for training (default: 100)')
parser.add_argument('--epochs', type=int, default=5000, metavar='N',
                    help='number of epochs to train (default: 10)')
parser.add_argument('--no-cuda', action='store_true', default=False,
                    help='enables CUDA training')


args = parser.parse_args()
args.cuda = not args.no_cuda and torch.cuda.is_available()

seed = 10
if args.cuda:
    torch.cuda.manual_seed(seed)
cuda = args.cuda

kwargs = {'num_workers': 1, 'pin_memory': True} if cuda else {}
n_classes = 10
z_dim = 10
X_dim = 784
y_dim = 10
convolutional = False
train_batch_size = args.batch_size
valid_batch_size = args.batch_size
N = 1000
epochs = args.epochs

##################################
# Load data and create Data loaders
##################################
def load_data(data_path='../data/'):
    print('loading data!')
    trainset_labeled = pickle.load(open(data_path + "train_labeled.p", "rb"))
    trainset_unlabeled = pickle.load(open(data_path + "train_unlabeled.p", "rb"))
    # Set -1 as labels for unlabeled data
    trainset_unlabeled.train_labels = torch.from_numpy(np.array([-1] * 47000))
    validset = pickle.load(open(data_path + "validation.p", "rb"))

    train_labeled_loader = torch.utils.data.DataLoader(trainset_labeled,
                                                       batch_size=train_batch_size,
                                                       shuffle=True, **kwargs)

    train_unlabeled_loader = torch.utils.data.DataLoader(trainset_unlabeled,
                                                         batch_size=train_batch_size,
                                                         shuffle=True, **kwargs)

    valid_loader = torch.utils.data.DataLoader(validset, batch_size=valid_batch_size, shuffle=True)

    return train_labeled_loader, train_unlabeled_loader, valid_loader

##################################
# Define Networks
##################################
# Encoder
class Q_net(nn.Module):
    def __init__(self):
        super(Q_net, self).__init__()
        self.lin1 = nn.Linear(X_dim, N)
        self.lin2 = nn.Linear(N, N)
        # Categorical code (y)
        self.lin3cat = nn.Linear(N, n_classes)
        # Gaussian code (z)
        self.lin3gauss = nn.Linear(N, z_dim)

    def forward(self, x):
        x = F.droppout(self.lin1(x), p=0.25, training=self.training)
        x = F.relu(x)
        x = F.droppout(self.lin2(x), p=0.25, training=self.training)
        x = F.relu(x)
        xcat = F.softmax(self.lin3cat(x))
        xgauss = self.lin3gauss(x)

        return xcat, xgauss

# Decoder
class P_net(nn.Module):
    def __init__(self):
        super(P_net, self).__init__()
        self.lin1 = nn.Linear(z_dim + n_classes, N)
        self.lin2 = nn.Linear(N, N)
        self.lin3 = nn.Linear(N, X_dim)

    def forward(self, x):
        x = self.lin1(x)
        x = F.dropout(x, p=0.25, training=self.training)
        x = F.relu(x)
        x = self.lin2(x)
        x = F.dropout(x, p=0.25, training=self.training)
        x = self.lin3(x)
        return F.sigmoid(x)


# Discriminator networks
class D_net_cat(nn.Module):
    def __init__(self):
        super(D_net_cat, self).__init__()
        self.lin1 = nn.Linear(n_classes, N)
        self.lin2 = nn.Linear(N, N)
        self.lin3 = nn.Linear(N, 1)

    def forward(self, x):
        x = F.dropout(self.lin1(x), p=0.2, training=self.training)
        x = F.relu(x)
        F.dropout(self.lin2(x), p=0.2, training=self.training)
        x = F.relu(x)
        return F.sigmoid(self.lin3(x))


class D_net_gauss(nn.Module):
    def __init__(self):
        super(D_net_gauss, self).__init__()
        self.lin1 = nn.Linear(z_dim, N)
        self.lin2 = nn.Linear(N, N)
        self.lin3 = nn.Linear(N, 1)

    def forward(self, x):
        x = F.dropout(self.lin1(x), p=0.2, training=self.training)
        x = F.relu(x)
        x = F.dropout(self.lin2(x), p=0.2, training=self.training)
        x = F.relu(x)

        return F.sigmoid(self.lin3(x))


####################
# Utility functions
####################
def save_model(model, filename):
    print('Best model so far, saving it...')
    torch.save(model.state_dict(), filename)

def sample_categorical(batch_size, n_classes=10):
    '''
     Sample from a categorical distribution
     of size batch_size and # of classes n_classes
     return: torch.autograd.Variable with the sample
    '''
    cat = np.random.randint(0, 10, batch_size)
    cat = np.eye(n_classes)[cat].astype('float32')
    cat = torch.from_numpy(cat)
    return Variable(cat)


def report_loss(epoch, D_loss_cat, D_loss_gauss, G_loss, recon_loss):
    '''
    Print loss
    '''
    print('Epoch-{}; D_loss_cat: {:.4}: D_loss_gauss: {:.4}; G_loss: {:.4}; recon_loss: {:.4}'.format(epoch,
                                                                                                      D_loss_cat.data[0],
                                                                                                      D_loss_gauss.data[0],
                                                                                                      G_loss.data[0],
                                                                                                      recon_loss.data[0]))


def create_latent(Q, loader):
    '''
    Creates the latent representation for the samples in loader
    return:
        z_values: numpy array with the latent representations
        labels: the labels corresponding to the latent representations
    '''
    Q.eval()
    labels = []

    for batch_idx, (X, target) in enumerate(loader):

        X = X * 0.3081 + 0.1307
        # X.resize_(loader.batch_size, X_dim)
        X, target = Variable(X), Variable(target)
        labels.extend(target.data.tolist())
        if cuda:
            X, target = X.cuda(), target.cuda()
        # Reconstruction phase
        z_sample = Q(X)
        if batch_idx > 0:
            z_values = np.concatenate((z_values, np.array(z_sample.data.tolist())))
        else:
            z_values = np.array(z_sample.data.tolist())
    labels = np.array(labels)

    return z_values, labels


def classification_accuracy(Q, data_loader):
    Q.eval()
    labels = []
    test_loss = 0
    correct = 0

    for batch_idx, (X, target) in enumerate(data_loader):
        X = X * 0.3081 + 0.1307
        X.resize_(data_loader.batch_size, X_dim)
        X, target = Variable(X), Variable(target)
        if cuda:
            X, target = X.cuda(), target.cuda()

        labels.extend(target.data.tolist())
        # Reconstruction phase
        output = Q(X)[0]

        test_loss += F.nll_loss(output, target).data[0]

        pred = output.data.max(1)[1]
        correct += pred.eq(target.data).cpu().sum()

    test_loss /= len(data_loader)
    return 100. * correct / len(data_loader.dataset)

def newdataset():
    z_cat = np.arange(0, n_classes)
    z_cat = np.eye(n_classes)[z_cat].astype('float32')
    z_cat = torch.from_numpy(z_cat)
    z_cat = Variable(z_cat)
    if cuda:
        z_cat = z_cat.cuda()
    
    z_real_gauss = Variable(torch.randn(10, z_dim))
    if cuda:
        z_real_gauss = z_real_gauss.cuda()
    X_sample = P(torch.cat((z_cat, z_real_gauss),1))

    for i in range(5000):
        z_real_gauss = Variable(torch.randn(10, z_dim))
        if cuda:
            z_real_gauss = z_real_gauss.cuda()
        x_sample = P(torch.cat((z_cat, z_real_gauss),1))
        X_sample = torch.cat((x_sample, X_sample),0)

    xs = np.array(X_sample.data.tolist())

    pkl.dump(xs, open('./synthetic_samples', 'w'))

####################
# Train procedure
####################
def train(P, Q, D_cat, D_gauss, P_decoder, Q_encoder, Q_semi_supervised, Q_generator, D_cat_solver, D_gauss_solver, train_labeled_loader, train_unlabeled_loader):
    '''
    Train procedure for one epoch.
    '''
    TINY = 1e-15
    # Set the networks in train mode (apply dropout when needed)
    Q.train()
    P.train()
    D_cat.train()
    D_gauss.train()

    if train_unlabeled_loader is None:
        train_unlabeled_loader = train_labeled_loader

    # Loop through the labeled and unlabeled dataset getting one batch of samples from each
    # The batch size has to be a divisor of the size of the dataset or it will return
    # invalid samples
    for (X_l, target_l), (X_u, target_u) in itertools.izip(train_labeled_loader, train_unlabeled_loader):

        for X, target in [(X_u, target_u), (X_l, target_l)]:
            if target[0] == -1:
                labeled = False
            else:
                labeled = True

            # Load batch and normalize samples to be between 0 and 1
            X = X * 0.3081 + 0.1307
            X.resize_(train_batch_size, X_dim)

            X, target = Variable(X), Variable(target)
            if cuda:
                X, target = X.cuda(), target.cuda()

            # Init gradients
            P.zero_grad()
            Q.zero_grad()
            D_cat.zero_grad()
            D_gauss.zero_grad()

            #######################
            # Reconstruction phase
            #######################
            if not labeled:
                z_sample = torch.cat(Q(X), 1)
                X_sample = P(z_sample)

                recon_loss = F.binary_cross_entropy(X_sample + TINY, X.resize(train_batch_size, X_dim) + TINY)
                recon_loss = recon_loss
                recon_loss.backward()
                P_decoder.step()
                Q_encoder.step()

                P.zero_grad()
                Q.zero_grad()
                D_cat.zero_grad()
                D_gauss.zero_grad()
                recon_loss = recon_loss
                #######################
                # Regularization phase
                #######################
                # Discriminator
                Q.eval()
                z_real_cat = sample_categorical(train_batch_size, n_classes=n_classes)
                z_real_gauss = Variable(torch.randn(train_batch_size, z_dim))
                if cuda:
                    z_real_cat = z_real_cat.cuda()
                    z_real_gauss = z_real_gauss.cuda()

                z_fake_cat, z_fake_gauss = Q(X)

                D_real_cat = D_cat(z_real_cat)
                D_real_gauss = D_gauss(z_real_gauss)
                D_fake_cat = D_cat(z_fake_cat)
                D_fake_gauss = D_gauss(z_fake_gauss)

                D_loss_cat = -torch.mean(torch.log(D_real_cat + TINY) + torch.log(1 - D_fake_cat + TINY))
                D_loss_gauss = -torch.mean(torch.log(D_real_gauss + TINY) + torch.log(1 - D_fake_gauss + TINY))

                D_loss = D_loss_cat + D_loss_gauss
                D_loss = D_loss

                D_loss.backward()
                D_cat_solver.step()
                D_gauss_solver.step()

                P.zero_grad()
                Q.zero_grad()
                D_cat.zero_grad()
                D_gauss.zero_grad()

                # Generator
                Q.train()
                z_fake_cat, z_fake_gauss = Q(X)

                D_fake_cat = D_cat(z_fake_cat)
                D_fake_gauss = D_gauss(z_fake_gauss)

                G_loss = - torch.mean(torch.log(D_fake_cat + TINY)) - torch.mean(torch.log(D_fake_gauss + TINY))
                G_loss = G_loss
                G_loss.backward()
                Q_generator.step()

                P.zero_grad()
                Q.zero_grad()
                D_cat.zero_grad()
                D_gauss.zero_grad()

            #######################
            # Semi-supervised phase
            #######################
            if labeled:
                pred, _ = Q(X)
                class_loss = F.cross_entropy(pred, target)
                class_loss.backward()
                Q_semi_supervised.step()

                P.zero_grad()
                Q.zero_grad()
                D_cat.zero_grad()
                D_gauss.zero_grad()

    return D_loss_cat, D_loss_gauss, G_loss, recon_loss, class_loss


def generate_model(savefolder='./'):
    train_labeled_loader, train_unlabeled_loader, valid_loader = load_data()

    # Create and initialize Networks
    train_acc = []
    val_acc = []

    torch.manual_seed(10)

    if cuda:
        Q = Q_net().cuda()
        P = P_net().cuda()
        D_cat = D_net_cat().cuda()
        D_gauss = D_net_gauss().cuda()
    else:
        Q = Q_net()
        P = P_net()
        D_gauss = D_net_gauss()
        D_cat = D_net_cat()

    # Set learning rates
    gen_lr = 0.0006
    semi_lr = 0.001
    reg_lr = 0.0008

    # Set optimizators
    P_decoder = optim.Adam(P.parameters(), lr=gen_lr)
    Q_encoder = optim.Adam(Q.parameters(), lr=gen_lr)

    Q_semi_supervised = optim.Adam(Q.parameters(), lr=semi_lr)

    Q_generator = optim.Adam(Q.parameters(), lr=reg_lr)
    D_gauss_solver = optim.Adam(D_gauss.parameters(), lr=reg_lr)
    D_cat_solver = optim.Adam(D_cat.parameters(), lr=reg_lr)

    for epoch in range(epochs):
        if epoch == 75:
            P_decoder = optim.Adam(P.parameters(), lr=gen_lr / 10.)
            Q_encoder = optim.Adam(Q.parameters(), lr=gen_lr / 10.)

            Q_semi_supervised = optim.Adam(Q.parameters(), lr=semi_lr / 10.)

            Q_generator = optim.Adam(Q.parameters(), lr=reg_lr / 10.)
            D_gauss_solver = optim.Adam(D_gauss.parameters(), lr=reg_lr / 10.)
            D_cat_solver = optim.Adam(D_cat.parameters(), lr=reg_lr / 10.)

        D_loss_cat, D_loss_gauss, G_loss, recon_loss, class_loss = train(P, Q, D_cat,
                                                                         D_gauss, P_decoder,
                                                                         Q_encoder, Q_semi_supervised,
                                                                         Q_generator,
                                                                         D_cat_solver, D_gauss_solver,
                                                                         train_labeled_loader,
                                                                         train_unlabeled_loader)
        if epoch % 10 == 0:
            train_acc.append(classification_accuracy(Q, train_labeled_loader))
            val_acc.append(classification_accuracy(Q, valid_loader))
            report_loss(epoch, D_loss_cat, D_loss_gauss, G_loss, recon_loss)
            print('Classification Loss: {:.3}'.format(class_loss.data[0]))
            print('Validation accuracy: {} %'.format(val_acc[-1]))

    torch.save(Q.state_dict(), savefolder + 'Q_networkL')
    torch.save(P.state_dict(), savefolder + 'P_network')
    torch.save(D_cat.state_dict(), savefolder + 'D_cat_network')
    torch.save(D_gauss.state_dict(), savefolder + 'D_gauss_network')


if __name__ == '__main__':
    generate_model()







import argparse
import time
import torch
import pickle
import numpy as np
import itertools
from torch.autograd import Variable
import torch.nn as nn
import torch.nn.functional as F
import torch.autograd as autograd
import torch.optim as optim
from torch.nn.modules.upsampling import UpsamplingNearest2d

# Training settings
parser = argparse.ArgumentParser(description='PyTorch semi-supervised MNIST')

parser.add_argument('--batch-size', type=int, default=100, metavar='N',
                    help='input batch size for training (default: 100)')
parser.add_argument('--epochs', type=int, default=5000, metavar='N',
                    help='number of epochs to train (default: 10)')
parser.add_argument('--no-cuda', action='store_true', default=False,
                    help='enables CUDA training')


args = parser.parse_args()
args.cuda = not args.no_cuda and torch.cuda.is_available()

seed = 10
if args.cuda:
    torch.cuda.manual_seed(seed)
cuda = args.cuda

kwargs = {'num_workers': 1, 'pin_memory': True} if cuda else {}
n_classes = 10
z_dim = 10
X_dim = 784
y_dim = 10
convolutional = False
train_batch_size = args.batch_size
valid_batch_size = args.batch_size
N = 1000
epochs = args.epochs

##################################
# Load data and create Data loaders
##################################
def load_data(data_path='../data/'):
    print('loading data!')
    trainset_labeled = pickle.load(open(data_path + "train_labeled.p", "rb"))
    trainset_unlabeled = pickle.load(open(data_path + "train_unlabeled.p", "rb"))
    # Set -1 as labels for unlabeled data
    trainset_unlabeled.train_labels = torch.from_numpy(np.array([-1] * 47000))
    validset = pickle.load(open(data_path + "validation.p", "rb"))

    train_labeled_loader = torch.utils.data.DataLoader(trainset_labeled,
                                                       batch_size=train_batch_size,
                                                       shuffle=True, **kwargs)

    train_unlabeled_loader = torch.utils.data.DataLoader(trainset_unlabeled,
                                                         batch_size=train_batch_size,
                                                         shuffle=True, **kwargs)

    valid_loader = torch.utils.data.DataLoader(validset, batch_size=valid_batch_size, shuffle=True)

    return train_labeled_loader, train_unlabeled_loader, valid_loader

##################################
# Define Networks
##################################
# Encoder
class Q_net(nn.Module):
    def __init__(self):
        super(Q_net, self).__init__()
        self.lin1 = nn.Linear(X_dim, N)
        self.lin2 = nn.Linear(N, N)
        # Categorical code (y)
        self.lin3cat = nn.Linear(N, n_classes)
        # Gaussian code (z)
        self.lin3gauss = nn.Linear(N, z_dim)

    def forward(self, x):
        x = F.droppout(self.lin1(x), p=0.25, training=self.training)
        x = F.relu(x)
        x = F.droppout(self.lin2(x), p=0.25, training=self.training)
        x = F.relu(x)
        xcat = F.softmax(self.lin3cat(x))
        xgauss = self.lin3gauss(x)

        return xcat, xgauss

# Decoder
class P_net(nn.Module):
    def __init__(self):
        super(P_net, self).__init__()
        self.lin1 = nn.Linear(z_dim + n_classes, N)
        self.lin2 = nn.Linear(N, N)
        self.lin3 = nn.Linear(N, X_dim)

    def forward(self, x):
        x = self.lin1(x)
        x = F.dropout(x, p=0.25, training=self.training)
        x = F.relu(x)
        x = self.lin2(x)
        x = F.dropout(x, p=0.25, training=self.training)
        x = self.lin3(x)
        return F.sigmoid(x)


# Discriminator networks
class D_net_cat(nn.Module):
    def __init__(self):
        super(D_net_cat, self).__init__()
        self.lin1 = nn.Linear(n_classes, N)
        self.lin2 = nn.Linear(N, N)
        self.lin3 = nn.Linear(N, 1)

    def forward(self, x):
        x = F.dropout(self.lin1(x), p=0.2, training=self.training)
        x = F.relu(x)
        F.dropout(self.lin2(x), p=0.2, training=self.training)
        x = F.relu(x)
        return F.sigmoid(self.lin3(x))


class D_net_gauss(nn.Module):
    def __init__(self):
        super(D_net_gauss, self).__init__()
        self.lin1 = nn.Linear(z_dim, N)
        self.lin2 = nn.Linear(N, N)
        self.lin3 = nn.Linear(N, 1)

    def forward(self, x):
        x = F.dropout(self.lin1(x), p=0.2, training=self.training)
        x = F.relu(x)
        x = F.dropout(self.lin2(x), p=0.2, training=self.training)
        x = F.relu(x)

        return F.sigmoid(self.lin3(x))


####################
# Utility functions
####################
def save_model(model, filename):
    print('Best model so far, saving it...')
    torch.save(model.state_dict(), filename)

def sample_categorical(batch_size, n_classes=10):
    '''
     Sample from a categorical distribution
     of size batch_size and # of classes n_classes
     return: torch.autograd.Variable with the sample
    '''
    cat = np.random.randint(0, 10, batch_size)
    cat = np.eye(n_classes)[cat].astype('float32')
    cat = torch.from_numpy(cat)
    return Variable(cat)


def report_loss(epoch, D_loss_cat, D_loss_gauss, G_loss, recon_loss):
    '''
    Print loss
    '''
    print('Epoch-{}; D_loss_cat: {:.4}: D_loss_gauss: {:.4}; G_loss: {:.4}; recon_loss: {:.4}'.format(epoch,
                                                                                                      D_loss_cat.data[0],
                                                                                                      D_loss_gauss.data[0],
                                                                                                      G_loss.data[0],
                                                                                                      recon_loss.data[0]))


def create_latent(Q, loader):
    '''
    Creates the latent representation for the samples in loader
    return:
        z_values: numpy array with the latent representations
        labels: the labels corresponding to the latent representations
    '''
    Q.eval()
    labels = []

    for batch_idx, (X, target) in enumerate(loader):

        X = X * 0.3081 + 0.1307
        # X.resize_(loader.batch_size, X_dim)
        X, target = Variable(X), Variable(target)
        labels.extend(target.data.tolist())
        if cuda:
            X, target = X.cuda(), target.cuda()
        # Reconstruction phase
        z_sample = Q(X)
        if batch_idx > 0:
            z_values = np.concatenate((z_values, np.array(z_sample.data.tolist())))
        else:
            z_values = np.array(z_sample.data.tolist())
    labels = np.array(labels)

    return z_values, labels


def classification_accuracy(Q, data_loader):
    Q.eval()
    labels = []
    test_loss = 0
    correct = 0

    for batch_idx, (X, target) in enumerate(data_loader):
        X = X * 0.3081 + 0.1307
        X.resize_(data_loader.batch_size, X_dim)
        X, target = Variable(X), Variable(target)
        if cuda:
            X, target = X.cuda(), target.cuda()

        labels.extend(target.data.tolist())
        # Reconstruction phase
        output = Q(X)[0]

        test_loss += F.nll_loss(output, target).data[0]

        pred = output.data.max(1)[1]
        correct += pred.eq(target.data).cpu().sum()

    test_loss /= len(data_loader)
    return 100. * correct / len(data_loader.dataset)

def newdataset():
    z_cat = np.arange(0, n_classes)
    z_cat = np.eye(n_classes)[z_cat].astype('float32')
    z_cat = torch.from_numpy(z_cat)
    z_cat = Variable(z_cat)
    if cuda:
        z_cat = z_cat.cuda()
    
    z_real_gauss = Variable(torch.randn(10, z_dim))
    if cuda:
        z_real_gauss = z_real_gauss.cuda()
    X_sample = P(torch.cat((z_cat, z_real_gauss),1))

    for i in range(5000):
        z_real_gauss = Variable(torch.randn(10, z_dim))
        if cuda:
            z_real_gauss = z_real_gauss.cuda()
        x_sample = P(torch.cat((z_cat, z_real_gauss),1))
        X_sample = torch.cat((x_sample, X_sample),0)

    xs = np.array(X_sample.data.tolist())

    pkl.dump(xs, open('./synthetic_samples', 'w'))

####################
# Train procedure
####################
def train(P, Q, D_cat, D_gauss, P_decoder, Q_encoder, Q_semi_supervised, Q_generator, D_cat_solver, D_gauss_solver, train_labeled_loader, train_unlabeled_loader):
    '''
    Train procedure for one epoch.
    '''
    TINY = 1e-15
    # Set the networks in train mode (apply dropout when needed)
    Q.train()
    P.train()
    D_cat.train()
    D_gauss.train()

    if train_unlabeled_loader is None:
        train_unlabeled_loader = train_labeled_loader

    # Loop through the labeled and unlabeled dataset getting one batch of samples from each
    # The batch size has to be a divisor of the size of the dataset or it will return
    # invalid samples
    for (X_l, target_l), (X_u, target_u) in itertools.izip(train_labeled_loader, train_unlabeled_loader):

        for X, target in [(X_u, target_u), (X_l, target_l)]:
            if target[0] == -1:
                labeled = False
            else:
                labeled = True

            # Load batch and normalize samples to be between 0 and 1
            X = X * 0.3081 + 0.1307
            X.resize_(train_batch_size, X_dim)

            X, target = Variable(X), Variable(target)
            if cuda:
                X, target = X.cuda(), target.cuda()

            # Init gradients
            P.zero_grad()
            Q.zero_grad()
            D_cat.zero_grad()
            D_gauss.zero_grad()

            #######################
            # Reconstruction phase
            #######################
            if not labeled:
                z_sample = torch.cat(Q(X), 1)
                X_sample = P(z_sample)

                recon_loss = F.binary_cross_entropy(X_sample + TINY, X.resize(train_batch_size, X_dim) + TINY)
                recon_loss = recon_loss
                recon_loss.backward()
                P_decoder.step()
                Q_encoder.step()

                P.zero_grad()
                Q.zero_grad()
                D_cat.zero_grad()
                D_gauss.zero_grad()
                recon_loss = recon_loss
                #######################
                # Regularization phase
                #######################
                # Discriminator
                Q.eval()
                z_real_cat = sample_categorical(train_batch_size, n_classes=n_classes)
                z_real_gauss = Variable(torch.randn(train_batch_size, z_dim))
                if cuda:
                    z_real_cat = z_real_cat.cuda()
                    z_real_gauss = z_real_gauss.cuda()

                z_fake_cat, z_fake_gauss = Q(X)

                D_real_cat = D_cat(z_real_cat)
                D_real_gauss = D_gauss(z_real_gauss)
                D_fake_cat = D_cat(z_fake_cat)
                D_fake_gauss = D_gauss(z_fake_gauss)

                D_loss_cat = -torch.mean(torch.log(D_real_cat + TINY) + torch.log(1 - D_fake_cat + TINY))
                D_loss_gauss = -torch.mean(torch.log(D_real_gauss + TINY) + torch.log(1 - D_fake_gauss + TINY))

                D_loss = D_loss_cat + D_loss_gauss
                D_loss = D_loss

                D_loss.backward()
                D_cat_solver.step()
                D_gauss_solver.step()

                P.zero_grad()
                Q.zero_grad()
                D_cat.zero_grad()
                D_gauss.zero_grad()

                # Generator
                Q.train()
                z_fake_cat, z_fake_gauss = Q(X)

                D_fake_cat = D_cat(z_fake_cat)
                D_fake_gauss = D_gauss(z_fake_gauss)

                G_loss = - torch.mean(torch.log(D_fake_cat + TINY)) - torch.mean(torch.log(D_fake_gauss + TINY))
                G_loss = G_loss
                G_loss.backward()
                Q_generator.step()

                P.zero_grad()
                Q.zero_grad()
                D_cat.zero_grad()
                D_gauss.zero_grad()

            #######################
            # Semi-supervised phase
            #######################
            if labeled:
                pred, _ = Q(X)
                class_loss = F.cross_entropy(pred, target)
                class_loss.backward()
                Q_semi_supervised.step()

                P.zero_grad()
                Q.zero_grad()
                D_cat.zero_grad()
                D_gauss.zero_grad()

    return D_loss_cat, D_loss_gauss, G_loss, recon_loss, class_loss


def generate_model(savefolder='./'):
    train_labeled_loader, train_unlabeled_loader, valid_loader = load_data()

    # Create and initialize Networks
    train_acc = []
    val_acc = []

    torch.manual_seed(10)

    if cuda:
        Q = Q_net().cuda()
        P = P_net().cuda()
        D_cat = D_net_cat().cuda()
        D_gauss = D_net_gauss().cuda()
    else:
        Q = Q_net()
        P = P_net()
        D_gauss = D_net_gauss()
        D_cat = D_net_cat()

    # Set learning rates
    gen_lr = 0.0006
    semi_lr = 0.001
    reg_lr = 0.0008

    # Set optimizators
    P_decoder = optim.Adam(P.parameters(), lr=gen_lr)
    Q_encoder = optim.Adam(Q.parameters(), lr=gen_lr)

    Q_semi_supervised = optim.Adam(Q.parameters(), lr=semi_lr)

    Q_generator = optim.Adam(Q.parameters(), lr=reg_lr)
    D_gauss_solver = optim.Adam(D_gauss.parameters(), lr=reg_lr)
    D_cat_solver = optim.Adam(D_cat.parameters(), lr=reg_lr)

    for epoch in range(epochs):
        if epoch == 75:
            P_decoder = optim.Adam(P.parameters(), lr=gen_lr / 10.)
            Q_encoder = optim.Adam(Q.parameters(), lr=gen_lr / 10.)

            Q_semi_supervised = optim.Adam(Q.parameters(), lr=semi_lr / 10.)

            Q_generator = optim.Adam(Q.parameters(), lr=reg_lr / 10.)
            D_gauss_solver = optim.Adam(D_gauss.parameters(), lr=reg_lr / 10.)
            D_cat_solver = optim.Adam(D_cat.parameters(), lr=reg_lr / 10.)

        D_loss_cat, D_loss_gauss, G_loss, recon_loss, class_loss = train(P, Q, D_cat,
                                                                         D_gauss, P_decoder,
                                                                         Q_encoder, Q_semi_supervised,
                                                                         Q_generator,
                                                                         D_cat_solver, D_gauss_solver,
                                                                         train_labeled_loader,
                                                                         train_unlabeled_loader)
        if epoch % 10 == 0:
            train_acc.append(classification_accuracy(Q, train_labeled_loader))
            val_acc.append(classification_accuracy(Q, valid_loader))
            report_loss(epoch, D_loss_cat, D_loss_gauss, G_loss, recon_loss)
            print('Classification Loss: {:.3}'.format(class_loss.data[0]))
            print('Validation accuracy: {} %'.format(val_acc[-1]))

    torch.save(Q.state_dict(), savefolder + 'Q_networkL')
    torch.save(P.state_dict(), savefolder + 'P_network')
    torch.save(D_cat.state_dict(), savefolder + 'D_cat_network')
    torch.save(D_gauss.state_dict(), savefolder + 'D_gauss_network')


if __name__ == '__main__':
    generate_model()







import argparse
import time
import torch
import pickle
import numpy as np
import itertools
from torch.autograd import Variable
import torch.nn as nn
import torch.nn.functional as F
import torch.autograd as autograd
import torch.optim as optim
from torch.nn.modules.upsampling import UpsamplingNearest2d

# Training settings
parser = argparse.ArgumentParser(description='PyTorch semi-supervised MNIST')

parser.add_argument('--batch-size', type=int, default=100, metavar='N',
                    help='input batch size for training (default: 100)')
parser.add_argument('--epochs', type=int, default=5000, metavar='N',
                    help='number of epochs to train (default: 10)')
parser.add_argument('--no-cuda', action='store_true', default=False,
                    help='enables CUDA training')


args = parser.parse_args()
args.cuda = not args.no_cuda and torch.cuda.is_available()

seed = 10
if args.cuda:
    torch.cuda.manual_seed(seed)
cuda = args.cuda

kwargs = {'num_workers': 1, 'pin_memory': True} if cuda else {}
n_classes = 10
z_dim = 10
X_dim = 784
y_dim = 10
convolutional = False
train_batch_size = args.batch_size
valid_batch_size = args.batch_size
N = 1000
epochs = args.epochs

##################################
# Load data and create Data loaders
##################################
def load_data(data_path='../data/'):
    print('loading data!')
    trainset_labeled = pickle.load(open(data_path + "train_labeled.p", "rb"))
    trainset_unlabeled = pickle.load(open(data_path + "train_unlabeled.p", "rb"))
    # Set -1 as labels for unlabeled data
    trainset_unlabeled.train_labels = torch.from_numpy(np.array([-1] * 47000))
    validset = pickle.load(open(data_path + "validation.p", "rb"))

    train_labeled_loader = torch.utils.data.DataLoader(trainset_labeled,
                                                       batch_size=train_batch_size,
                                                       shuffle=True, **kwargs)

    train_unlabeled_loader = torch.utils.data.DataLoader(trainset_unlabeled,
                                                         batch_size=train_batch_size,
                                                         shuffle=True, **kwargs)

    valid_loader = torch.utils.data.DataLoader(validset, batch_size=valid_batch_size, shuffle=True)

    return train_labeled_loader, train_unlabeled_loader, valid_loader

##################################
# Define Networks
##################################
# Encoder
class Q_net(nn.Module):
    def __init__(self):
        super(Q_net, self).__init__()
        self.lin1 = nn.Linear(X_dim, N)
        self.lin2 = nn.Linear(N, N)
        # Categorical code (y)
        self.lin3cat = nn.Linear(N, n_classes)
        # Gaussian code (z)
        self.lin3gauss = nn.Linear(N, z_dim)

    def forward(self, x):
        x = F.droppout(self.lin1(x), p=0.25, training=self.training)
        x = F.relu(x)
        x = F.droppout(self.lin2(x), p=0.25, training=self.training)
        x = F.relu(x)
        xcat = F.softmax(self.lin3cat(x))
        xgauss = self.lin3gauss(x)

        return xcat, xgauss

# Decoder
class P_net(nn.Module):
    def __init__(self):
        super(P_net, self).__init__()
        self.lin1 = nn.Linear(z_dim + n_classes, N)
        self.lin2 = nn.Linear(N, N)
        self.lin3 = nn.Linear(N, X_dim)

    def forward(self, x):
        x = self.lin1(x)
        x = F.dropout(x, p=0.25, training=self.training)
        x = F.relu(x)
        x = self.lin2(x)
        x = F.dropout(x, p=0.25, training=self.training)
        x = self.lin3(x)
        return F.sigmoid(x)


# Discriminator networks
class D_net_cat(nn.Module):
    def __init__(self):
        super(D_net_cat, self).__init__()
        self.lin1 = nn.Linear(n_classes, N)
        self.lin2 = nn.Linear(N, N)
        self.lin3 = nn.Linear(N, 1)

    def forward(self, x):
        x = F.dropout(self.lin1(x), p=0.2, training=self.training)
        x = F.relu(x)
        F.dropout(self.lin2(x), p=0.2, training=self.training)
        x = F.relu(x)
        return F.sigmoid(self.lin3(x))


class D_net_gauss(nn.Module):
    def __init__(self):
        super(D_net_gauss, self).__init__()
        self.lin1 = nn.Linear(z_dim, N)
        self.lin2 = nn.Linear(N, N)
        self.lin3 = nn.Linear(N, 1)

    def forward(self, x):
        x = F.dropout(self.lin1(x), p=0.2, training=self.training)
        x = F.relu(x)
        x = F.dropout(self.lin2(x), p=0.2, training=self.training)
        x = F.relu(x)

        return F.sigmoid(self.lin3(x))


####################
# Utility functions
####################
def save_model(model, filename):
    print('Best model so far, saving it...')
    torch.save(model.state_dict(), filename)

def sample_categorical(batch_size, n_classes=10):
    '''
     Sample from a categorical distribution
     of size batch_size and # of classes n_classes
     return: torch.autograd.Variable with the sample
    '''
    cat = np.random.randint(0, 10, batch_size)
    cat = np.eye(n_classes)[cat].astype('float32')
    cat = torch.from_numpy(cat)
    return Variable(cat)


def report_loss(epoch, D_loss_cat, D_loss_gauss, G_loss, recon_loss):
    '''
    Print loss
    '''
    print('Epoch-{}; D_loss_cat: {:.4}: D_loss_gauss: {:.4}; G_loss: {:.4}; recon_loss: {:.4}'.format(epoch,
                                                                                                      D_loss_cat.data[0],
                                                                                                      D_loss_gauss.data[0],
                                                                                                      G_loss.data[0],
                                                                                                      recon_loss.data[0]))


def create_latent(Q, loader):
    '''
    Creates the latent representation for the samples in loader
    return:
        z_values: numpy array with the latent representations
        labels: the labels corresponding to the latent representations
    '''
    Q.eval()
    labels = []

    for batch_idx, (X, target) in enumerate(loader):

        X = X * 0.3081 + 0.1307
        # X.resize_(loader.batch_size, X_dim)
        X, target = Variable(X), Variable(target)
        labels.extend(target.data.tolist())
        if cuda:
            X, target = X.cuda(), target.cuda()
        # Reconstruction phase
        z_sample = Q(X)
        if batch_idx > 0:
            z_values = np.concatenate((z_values, np.array(z_sample.data.tolist())))
        else:
            z_values = np.array(z_sample.data.tolist())
    labels = np.array(labels)

    return z_values, labels


def classification_accuracy(Q, data_loader):
    Q.eval()
    labels = []
    test_loss = 0
    correct = 0

    for batch_idx, (X, target) in enumerate(data_loader):
        X = X * 0.3081 + 0.1307
        X.resize_(data_loader.batch_size, X_dim)
        X, target = Variable(X), Variable(target)
        if cuda:
            X, target = X.cuda(), target.cuda()

        labels.extend(target.data.tolist())
        # Reconstruction phase
        output = Q(X)[0]

        test_loss += F.nll_loss(output, target).data[0]

        pred = output.data.max(1)[1]
        correct += pred.eq(target.data).cpu().sum()

    test_loss /= len(data_loader)
    return 100. * correct / len(data_loader.dataset)

def newdataset():
    z_cat = np.arange(0, n_classes)
    z_cat = np.eye(n_classes)[z_cat].astype('float32')
    z_cat = torch.from_numpy(z_cat)
    z_cat = Variable(z_cat)
    if cuda:
        z_cat = z_cat.cuda()
    
    z_real_gauss = Variable(torch.randn(10, z_dim))
    if cuda:
        z_real_gauss = z_real_gauss.cuda()
    X_sample = P(torch.cat((z_cat, z_real_gauss),1))

    for i in range(5000):
        z_real_gauss = Variable(torch.randn(10, z_dim))
        if cuda:
            z_real_gauss = z_real_gauss.cuda()
        x_sample = P(torch.cat((z_cat, z_real_gauss),1))
        X_sample = torch.cat((x_sample, X_sample),0)

    xs = np.array(X_sample.data.tolist())

    pkl.dump(xs, open('./synthetic_samples', 'w'))

####################
# Train procedure
####################
def train(P, Q, D_cat, D_gauss, P_decoder, Q_encoder, Q_semi_supervised, Q_generator, D_cat_solver, D_gauss_solver, train_labeled_loader, train_unlabeled_loader):
    '''
    Train procedure for one epoch.
    '''
    TINY = 1e-15
    # Set the networks in train mode (apply dropout when needed)
    Q.train()
    P.train()
    D_cat.train()
    D_gauss.train()

    if train_unlabeled_loader is None:
        train_unlabeled_loader = train_labeled_loader

    # Loop through the labeled and unlabeled dataset getting one batch of samples from each
    # The batch size has to be a divisor of the size of the dataset or it will return
    # invalid samples
    for (X_l, target_l), (X_u, target_u) in itertools.izip(train_labeled_loader, train_unlabeled_loader):

        for X, target in [(X_u, target_u), (X_l, target_l)]:
            if target[0] == -1:
                labeled = False
            else:
                labeled = True

            # Load batch and normalize samples to be between 0 and 1
            X = X * 0.3081 + 0.1307
            X.resize_(train_batch_size, X_dim)

            X, target = Variable(X), Variable(target)
            if cuda:
                X, target = X.cuda(), target.cuda()

            # Init gradients
            P.zero_grad()
            Q.zero_grad()
            D_cat.zero_grad()
            D_gauss.zero_grad()

            #######################
            # Reconstruction phase
            #######################
            if not labeled:
                z_sample = torch.cat(Q(X), 1)
                X_sample = P(z_sample)

                recon_loss = F.binary_cross_entropy(X_sample + TINY, X.resize(train_batch_size, X_dim) + TINY)
                recon_loss = recon_loss
                recon_loss.backward()
                P_decoder.step()
                Q_encoder.step()

                P.zero_grad()
                Q.zero_grad()
                D_cat.zero_grad()
                D_gauss.zero_grad()
                recon_loss = recon_loss
                #######################
                # Regularization phase
                #######################
                # Discriminator
                Q.eval()
                z_real_cat = sample_categorical(train_batch_size, n_classes=n_classes)
                z_real_gauss = Variable(torch.randn(train_batch_size, z_dim))
                if cuda:
                    z_real_cat = z_real_cat.cuda()
                    z_real_gauss = z_real_gauss.cuda()

                z_fake_cat, z_fake_gauss = Q(X)

                D_real_cat = D_cat(z_real_cat)
                D_real_gauss = D_gauss(z_real_gauss)
                D_fake_cat = D_cat(z_fake_cat)
                D_fake_gauss = D_gauss(z_fake_gauss)

                D_loss_cat = -torch.mean(torch.log(D_real_cat + TINY) + torch.log(1 - D_fake_cat + TINY))
                D_loss_gauss = -torch.mean(torch.log(D_real_gauss + TINY) + torch.log(1 - D_fake_gauss + TINY))

                D_loss = D_loss_cat + D_loss_gauss
                D_loss = D_loss

                D_loss.backward()
                D_cat_solver.step()
                D_gauss_solver.step()

                P.zero_grad()
                Q.zero_grad()
                D_cat.zero_grad()
                D_gauss.zero_grad()

                # Generator
                Q.train()
                z_fake_cat, z_fake_gauss = Q(X)

                D_fake_cat = D_cat(z_fake_cat)
                D_fake_gauss = D_gauss(z_fake_gauss)

                G_loss = - torch.mean(torch.log(D_fake_cat + TINY)) - torch.mean(torch.log(D_fake_gauss + TINY))
                G_loss = G_loss
                G_loss.backward()
                Q_generator.step()

                P.zero_grad()
                Q.zero_grad()
                D_cat.zero_grad()
                D_gauss.zero_grad()

            #######################
            # Semi-supervised phase
            #######################
            if labeled:
                pred, _ = Q(X)
                class_loss = F.cross_entropy(pred, target)
                class_loss.backward()
                Q_semi_supervised.step()

                P.zero_grad()
                Q.zero_grad()
                D_cat.zero_grad()
                D_gauss.zero_grad()

    return D_loss_cat, D_loss_gauss, G_loss, recon_loss, class_loss


def generate_model(savefolder='./'):
    train_labeled_loader, train_unlabeled_loader, valid_loader = load_data()

    # Create and initialize Networks
    train_acc = []
    val_acc = []

    torch.manual_seed(10)

    if cuda:
        Q = Q_net().cuda()
        P = P_net().cuda()
        D_cat = D_net_cat().cuda()
        D_gauss = D_net_gauss().cuda()
    else:
        Q = Q_net()
        P = P_net()
        D_gauss = D_net_gauss()
        D_cat = D_net_cat()

    # Set learning rates
    gen_lr = 0.0006
    semi_lr = 0.001
    reg_lr = 0.0008

    # Set optimizators
    P_decoder = optim.Adam(P.parameters(), lr=gen_lr)
    Q_encoder = optim.Adam(Q.parameters(), lr=gen_lr)

    Q_semi_supervised = optim.Adam(Q.parameters(), lr=semi_lr)

    Q_generator = optim.Adam(Q.parameters(), lr=reg_lr)
    D_gauss_solver = optim.Adam(D_gauss.parameters(), lr=reg_lr)
    D_cat_solver = optim.Adam(D_cat.parameters(), lr=reg_lr)

    for epoch in range(epochs):
        if epoch == 75:
            P_decoder = optim.Adam(P.parameters(), lr=gen_lr / 10.)
            Q_encoder = optim.Adam(Q.parameters(), lr=gen_lr / 10.)

            Q_semi_supervised = optim.Adam(Q.parameters(), lr=semi_lr / 10.)

            Q_generator = optim.Adam(Q.parameters(), lr=reg_lr / 10.)
            D_gauss_solver = optim.Adam(D_gauss.parameters(), lr=reg_lr / 10.)
            D_cat_solver = optim.Adam(D_cat.parameters(), lr=reg_lr / 10.)

        D_loss_cat, D_loss_gauss, G_loss, recon_loss, class_loss = train(P, Q, D_cat,
                                                                         D_gauss, P_decoder,
                                                                         Q_encoder, Q_semi_supervised,
                                                                         Q_generator,
                                                                         D_cat_solver, D_gauss_solver,
                                                                         train_labeled_loader,
                                                                         train_unlabeled_loader)
        if epoch % 10 == 0:
            train_acc.append(classification_accuracy(Q, train_labeled_loader))
            val_acc.append(classification_accuracy(Q, valid_loader))
            report_loss(epoch, D_loss_cat, D_loss_gauss, G_loss, recon_loss)
            print('Classification Loss: {:.3}'.format(class_loss.data[0]))
            print('Validation accuracy: {} %'.format(val_acc[-1]))

    torch.save(Q.state_dict(), savefolder + 'Q_networkL')
    torch.save(P.state_dict(), savefolder + 'P_network')
    torch.save(D_cat.state_dict(), savefolder + 'D_cat_network')
    torch.save(D_gauss.state_dict(), savefolder + 'D_gauss_network')


if __name__ == '__main__':
    generate_model()







import argparse
import time
import torch
import pickle
import numpy as np
import itertools
from torch.autograd import Variable
import torch.nn as nn
import torch.nn.functional as F
import torch.autograd as autograd
import torch.optim as optim
from torch.nn.modules.upsampling import UpsamplingNearest2d

# Training settings
parser = argparse.ArgumentParser(description='PyTorch semi-supervised MNIST')

parser.add_argument('--batch-size', type=int, default=100, metavar='N',
                    help='input batch size for training (default: 100)')
parser.add_argument('--epochs', type=int, default=5000, metavar='N',
                    help='number of epochs to train (default: 10)')
parser.add_argument('--no-cuda', action='store_true', default=False,
                    help='enables CUDA training')


args = parser.parse_args()
args.cuda = not args.no_cuda and torch.cuda.is_available()

seed = 10
if args.cuda:
    torch.cuda.manual_seed(seed)
cuda = args.cuda

kwargs = {'num_workers': 1, 'pin_memory': True} if cuda else {}
n_classes = 10
z_dim = 10
X_dim = 784
y_dim = 10
convolutional = False
train_batch_size = args.batch_size
valid_batch_size = args.batch_size
N = 1000
epochs = args.epochs

##################################
# Load data and create Data loaders
##################################
def load_data(data_path='../data/'):
    print('loading data!')
    trainset_labeled = pickle.load(open(data_path + "train_labeled.p", "rb"))
    trainset_unlabeled = pickle.load(open(data_path + "train_unlabeled.p", "rb"))
    # Set -1 as labels for unlabeled data
    trainset_unlabeled.train_labels = torch.from_numpy(np.array([-1] * 47000))
    validset = pickle.load(open(data_path + "validation.p", "rb"))

    train_labeled_loader = torch.utils.data.DataLoader(trainset_labeled,
                                                       batch_size=train_batch_size,
                                                       shuffle=True, **kwargs)

    train_unlabeled_loader = torch.utils.data.DataLoader(trainset_unlabeled,
                                                         batch_size=train_batch_size,
                                                         shuffle=True, **kwargs)

    valid_loader = torch.utils.data.DataLoader(validset, batch_size=valid_batch_size, shuffle=True)

    return train_labeled_loader, train_unlabeled_loader, valid_loader

##################################
# Define Networks
##################################
# Encoder
class Q_net(nn.Module):
    def __init__(self):
        super(Q_net, self).__init__()
        self.lin1 = nn.Linear(X_dim, N)
        self.lin2 = nn.Linear(N, N)
        # Categorical code (y)
        self.lin3cat = nn.Linear(N, n_classes)
        # Gaussian code (z)
        self.lin3gauss = nn.Linear(N, z_dim)

    def forward(self, x):
        x = F.droppout(self.lin1(x), p=0.25, training=self.training)
        x = F.relu(x)
        x = F.droppout(self.lin2(x), p=0.25, training=self.training)
        x = F.relu(x)
        xcat = F.softmax(self.lin3cat(x))
        xgauss = self.lin3gauss(x)

        return xcat, xgauss

# Decoder
class P_net(nn.Module):
    def __init__(self):
        super(P_net, self).__init__()
        self.lin1 = nn.Linear(z_dim + n_classes, N)
        self.lin2 = nn.Linear(N, N)
        self.lin3 = nn.Linear(N, X_dim)

    def forward(self, x):
        x = self.lin1(x)
        x = F.dropout(x, p=0.25, training=self.training)
        x = F.relu(x)
        x = self.lin2(x)
        x = F.dropout(x, p=0.25, training=self.training)
        x = self.lin3(x)
        return F.sigmoid(x)


# Discriminator networks
class D_net_cat(nn.Module):
    def __init__(self):
        super(D_net_cat, self).__init__()
        self.lin1 = nn.Linear(n_classes, N)
        self.lin2 = nn.Linear(N, N)
        self.lin3 = nn.Linear(N, 1)

    def forward(self, x):
        x = F.dropout(self.lin1(x), p=0.2, training=self.training)
        x = F.relu(x)
        F.dropout(self.lin2(x), p=0.2, training=self.training)
        x = F.relu(x)
        return F.sigmoid(self.lin3(x))


class D_net_gauss(nn.Module):
    def __init__(self):
        super(D_net_gauss, self).__init__()
        self.lin1 = nn.Linear(z_dim, N)
        self.lin2 = nn.Linear(N, N)
        self.lin3 = nn.Linear(N, 1)

    def forward(self, x):
        x = F.dropout(self.lin1(x), p=0.2, training=self.training)
        x = F.relu(x)
        x = F.dropout(self.lin2(x), p=0.2, training=self.training)
        x = F.relu(x)

        return F.sigmoid(self.lin3(x))


####################
# Utility functions
####################
def save_model(model, filename):
    print('Best model so far, saving it...')
    torch.save(model.state_dict(), filename)

def sample_categorical(batch_size, n_classes=10):
    '''
     Sample from a categorical distribution
     of size batch_size and # of classes n_classes
     return: torch.autograd.Variable with the sample
    '''
    cat = np.random.randint(0, 10, batch_size)
    cat = np.eye(n_classes)[cat].astype('float32')
    cat = torch.from_numpy(cat)
    return Variable(cat)


def report_loss(epoch, D_loss_cat, D_loss_gauss, G_loss, recon_loss):
    '''
    Print loss
    '''
    print('Epoch-{}; D_loss_cat: {:.4}: D_loss_gauss: {:.4}; G_loss: {:.4}; recon_loss: {:.4}'.format(epoch,
                                                                                                      D_loss_cat.data[0],
                                                                                                      D_loss_gauss.data[0],
                                                                                                      G_loss.data[0],
                                                                                                      recon_loss.data[0]))


def create_latent(Q, loader):
    '''
    Creates the latent representation for the samples in loader
    return:
        z_values: numpy array with the latent representations
        labels: the labels corresponding to the latent representations
    '''
    Q.eval()
    labels = []

    for batch_idx, (X, target) in enumerate(loader):

        X = X * 0.3081 + 0.1307
        # X.resize_(loader.batch_size, X_dim)
        X, target = Variable(X), Variable(target)
        labels.extend(target.data.tolist())
        if cuda:
            X, target = X.cuda(), target.cuda()
        # Reconstruction phase
        z_sample = Q(X)
        if batch_idx > 0:
            z_values = np.concatenate((z_values, np.array(z_sample.data.tolist())))
        else:
            z_values = np.array(z_sample.data.tolist())
    labels = np.array(labels)

    return z_values, labels


def classification_accuracy(Q, data_loader):
    Q.eval()
    labels = []
    test_loss = 0
    correct = 0

    for batch_idx, (X, target) in enumerate(data_loader):
        X = X * 0.3081 + 0.1307
        X.resize_(data_loader.batch_size, X_dim)
        X, target = Variable(X), Variable(target)
        if cuda:
            X, target = X.cuda(), target.cuda()

        labels.extend(target.data.tolist())
        # Reconstruction phase
        output = Q(X)[0]

        test_loss += F.nll_loss(output, target).data[0]

        pred = output.data.max(1)[1]
        correct += pred.eq(target.data).cpu().sum()

    test_loss /= len(data_loader)
    return 100. * correct / len(data_loader.dataset)

def newdataset():
    z_cat = np.arange(0, n_classes)
    z_cat = np.eye(n_classes)[z_cat].astype('float32')
    z_cat = torch.from_numpy(z_cat)
    z_cat = Variable(z_cat)
    if cuda:
        z_cat = z_cat.cuda()
    
    z_real_gauss = Variable(torch.randn(10, z_dim))
    if cuda:
        z_real_gauss = z_real_gauss.cuda()
    X_sample = P(torch.cat((z_cat, z_real_gauss),1))

    for i in range(5000):
        z_real_gauss = Variable(torch.randn(10, z_dim))
        if cuda:
            z_real_gauss = z_real_gauss.cuda()
        x_sample = P(torch.cat((z_cat, z_real_gauss),1))
        X_sample = torch.cat((x_sample, X_sample),0)

    xs = np.array(X_sample.data.tolist())

    pkl.dump(xs, open('./synthetic_samples', 'w'))

####################
# Train procedure
####################
def train(P, Q, D_cat, D_gauss, P_decoder, Q_encoder, Q_semi_supervised, Q_generator, D_cat_solver, D_gauss_solver, train_labeled_loader, train_unlabeled_loader):
    '''
    Train procedure for one epoch.
    '''
    TINY = 1e-15
    # Set the networks in train mode (apply dropout when needed)
    Q.train()
    P.train()
    D_cat.train()
    D_gauss.train()

    if train_unlabeled_loader is None:
        train_unlabeled_loader = train_labeled_loader

    # Loop through the labeled and unlabeled dataset getting one batch of samples from each
    # The batch size has to be a divisor of the size of the dataset or it will return
    # invalid samples
    for (X_l, target_l), (X_u, target_u) in itertools.izip(train_labeled_loader, train_unlabeled_loader):

        for X, target in [(X_u, target_u), (X_l, target_l)]:
            if target[0] == -1:
                labeled = False
            else:
                labeled = True

            # Load batch and normalize samples to be between 0 and 1
            X = X * 0.3081 + 0.1307
            X.resize_(train_batch_size, X_dim)

            X, target = Variable(X), Variable(target)
            if cuda:
                X, target = X.cuda(), target.cuda()

            # Init gradients
            P.zero_grad()
            Q.zero_grad()
            D_cat.zero_grad()
            D_gauss.zero_grad()

            #######################
            # Reconstruction phase
            #######################
            if not labeled:
                z_sample = torch.cat(Q(X), 1)
                X_sample = P(z_sample)

                recon_loss = F.binary_cross_entropy(X_sample + TINY, X.resize(train_batch_size, X_dim) + TINY)
                recon_loss = recon_loss
                recon_loss.backward()
                P_decoder.step()
                Q_encoder.step()

                P.zero_grad()
                Q.zero_grad()
                D_cat.zero_grad()
                D_gauss.zero_grad()
                recon_loss = recon_loss
                #######################
                # Regularization phase
                #######################
                # Discriminator
                Q.eval()
                z_real_cat = sample_categorical(train_batch_size, n_classes=n_classes)
                z_real_gauss = Variable(torch.randn(train_batch_size, z_dim))
                if cuda:
                    z_real_cat = z_real_cat.cuda()
                    z_real_gauss = z_real_gauss.cuda()

                z_fake_cat, z_fake_gauss = Q(X)

                D_real_cat = D_cat(z_real_cat)
                D_real_gauss = D_gauss(z_real_gauss)
                D_fake_cat = D_cat(z_fake_cat)
                D_fake_gauss = D_gauss(z_fake_gauss)

                D_loss_cat = -torch.mean(torch.log(D_real_cat + TINY) + torch.log(1 - D_fake_cat + TINY))
                D_loss_gauss = -torch.mean(torch.log(D_real_gauss + TINY) + torch.log(1 - D_fake_gauss + TINY))

                D_loss = D_loss_cat + D_loss_gauss
                D_loss = D_loss

                D_loss.backward()
                D_cat_solver.step()
                D_gauss_solver.step()

                P.zero_grad()
                Q.zero_grad()
                D_cat.zero_grad()
                D_gauss.zero_grad()

                # Generator
                Q.train()
                z_fake_cat, z_fake_gauss = Q(X)

                D_fake_cat = D_cat(z_fake_cat)
                D_fake_gauss = D_gauss(z_fake_gauss)

                G_loss = - torch.mean(torch.log(D_fake_cat + TINY)) - torch.mean(torch.log(D_fake_gauss + TINY))
                G_loss = G_loss
                G_loss.backward()
                Q_generator.step()

                P.zero_grad()
                Q.zero_grad()
                D_cat.zero_grad()
                D_gauss.zero_grad()

            #######################
            # Semi-supervised phase
            #######################
            if labeled:
                pred, _ = Q(X)
                class_loss = F.cross_entropy(pred, target)
                class_loss.backward()
                Q_semi_supervised.step()

                P.zero_grad()
                Q.zero_grad()
                D_cat.zero_grad()
                D_gauss.zero_grad()

    return D_loss_cat, D_loss_gauss, G_loss, recon_loss, class_loss


def generate_model(savefolder='./'):
    train_labeled_loader, train_unlabeled_loader, valid_loader = load_data()

    # Create and initialize Networks
    train_acc = []
    val_acc = []

    torch.manual_seed(10)

    if cuda:
        Q = Q_net().cuda()
        P = P_net().cuda()
        D_cat = D_net_cat().cuda()
        D_gauss = D_net_gauss().cuda()
    else:
        Q = Q_net()
        P = P_net()
        D_gauss = D_net_gauss()
        D_cat = D_net_cat()

    # Set learning rates
    gen_lr = 0.0006
    semi_lr = 0.001
    reg_lr = 0.0008

    # Set optimizators
    P_decoder = optim.Adam(P.parameters(), lr=gen_lr)
    Q_encoder = optim.Adam(Q.parameters(), lr=gen_lr)

    Q_semi_supervised = optim.Adam(Q.parameters(), lr=semi_lr)

    Q_generator = optim.Adam(Q.parameters(), lr=reg_lr)
    D_gauss_solver = optim.Adam(D_gauss.parameters(), lr=reg_lr)
    D_cat_solver = optim.Adam(D_cat.parameters(), lr=reg_lr)

    for epoch in range(epochs):
        if epoch == 75:
            P_decoder = optim.Adam(P.parameters(), lr=gen_lr / 10.)
            Q_encoder = optim.Adam(Q.parameters(), lr=gen_lr / 10.)

            Q_semi_supervised = optim.Adam(Q.parameters(), lr=semi_lr / 10.)

            Q_generator = optim.Adam(Q.parameters(), lr=reg_lr / 10.)
            D_gauss_solver = optim.Adam(D_gauss.parameters(), lr=reg_lr / 10.)
            D_cat_solver = optim.Adam(D_cat.parameters(), lr=reg_lr / 10.)

        D_loss_cat, D_loss_gauss, G_loss, recon_loss, class_loss = train(P, Q, D_cat,
                                                                         D_gauss, P_decoder,
                                                                         Q_encoder, Q_semi_supervised,
                                                                         Q_generator,
                                                                         D_cat_solver, D_gauss_solver,
                                                                         train_labeled_loader,
                                                                         train_unlabeled_loader)
        if epoch % 10 == 0:
            train_acc.append(classification_accuracy(Q, train_labeled_loader))
            val_acc.append(classification_accuracy(Q, valid_loader))
            report_loss(epoch, D_loss_cat, D_loss_gauss, G_loss, recon_loss)
            print('Classification Loss: {:.3}'.format(class_loss.data[0]))
            print('Validation accuracy: {} %'.format(val_acc[-1]))

    torch.save(Q.state_dict(), savefolder + 'Q_networkL')
    torch.save(P.state_dict(), savefolder + 'P_network')
    torch.save(D_cat.state_dict(), savefolder + 'D_cat_network')
    torch.save(D_gauss.state_dict(), savefolder + 'D_gauss_network')


if __name__ == '__main__':
    generate_model()







import argparse
import time
import torch
import pickle
import numpy as np
import itertools
from torch.autograd import Variable
import torch.nn as nn
import torch.nn.functional as F
import torch.autograd as autograd
import torch.optim as optim
from torch.nn.modules.upsampling import UpsamplingNearest2d

# Training settings
parser = argparse.ArgumentParser(description='PyTorch semi-supervised MNIST')

parser.add_argument('--batch-size', type=int, default=100, metavar='N',
                    help='input batch size for training (default: 100)')
parser.add_argument('--epochs', type=int, default=5000, metavar='N',
                    help='number of epochs to train (default: 10)')
parser.add_argument('--no-cuda', action='store_true', default=False,
                    help='enables CUDA training')


args = parser.parse_args()
args.cuda = not args.no_cuda and torch.cuda.is_available()

seed = 10
if args.cuda:
    torch.cuda.manual_seed(seed)
cuda = args.cuda

kwargs = {'num_workers': 1, 'pin_memory': True} if cuda else {}
n_classes = 10
z_dim = 10
X_dim = 784
y_dim = 10
convolutional = False
train_batch_size = args.batch_size
valid_batch_size = args.batch_size
N = 1000
epochs = args.epochs

##################################
# Load data and create Data loaders
##################################
def load_data(data_path='../data/'):
    print('loading data!')
    trainset_labeled = pickle.load(open(data_path + "train_labeled.p", "rb"))
    trainset_unlabeled = pickle.load(open(data_path + "train_unlabeled.p", "rb"))
    # Set -1 as labels for unlabeled data
    trainset_unlabeled.train_labels = torch.from_numpy(np.array([-1] * 47000))
    validset = pickle.load(open(data_path + "validation.p", "rb"))

    train_labeled_loader = torch.utils.data.DataLoader(trainset_labeled,
                                                       batch_size=train_batch_size,
                                                       shuffle=True, **kwargs)

    train_unlabeled_loader = torch.utils.data.DataLoader(trainset_unlabeled,
                                                         batch_size=train_batch_size,
                                                         shuffle=True, **kwargs)

    valid_loader = torch.utils.data.DataLoader(validset, batch_size=valid_batch_size, shuffle=True)

    return train_labeled_loader, train_unlabeled_loader, valid_loader

##################################
# Define Networks
##################################
# Encoder
class Q_net(nn.Module):
    def __init__(self):
        super(Q_net, self).__init__()
        self.lin1 = nn.Linear(X_dim, N)
        self.lin2 = nn.Linear(N, N)
        # Categorical code (y)
        self.lin3cat = nn.Linear(N, n_classes)
        # Gaussian code (z)
        self.lin3gauss = nn.Linear(N, z_dim)

    def forward(self, x):
        x = F.droppout(self.lin1(x), p=0.25, training=self.training)
        x = F.relu(x)
        x = F.droppout(self.lin2(x), p=0.25, training=self.training)
        x = F.relu(x)
        xcat = F.softmax(self.lin3cat(x))
        xgauss = self.lin3gauss(x)

        return xcat, xgauss

# Decoder
class P_net(nn.Module):
    def __init__(self):
        super(P_net, self).__init__()
        self.lin1 = nn.Linear(z_dim + n_classes, N)
        self.lin2 = nn.Linear(N, N)
        self.lin3 = nn.Linear(N, X_dim)

    def forward(self, x):
        x = self.lin1(x)
        x = F.dropout(x, p=0.25, training=self.training)
        x = F.relu(x)
        x = self.lin2(x)
        x = F.dropout(x, p=0.25, training=self.training)
        x = self.lin3(x)
        return F.sigmoid(x)


# Discriminator networks
class D_net_cat(nn.Module):
    def __init__(self):
        super(D_net_cat, self).__init__()
        self.lin1 = nn.Linear(n_classes, N)
        self.lin2 = nn.Linear(N, N)
        self.lin3 = nn.Linear(N, 1)

    def forward(self, x):
        x = F.dropout(self.lin1(x), p=0.2, training=self.training)
        x = F.relu(x)
        F.dropout(self.lin2(x), p=0.2, training=self.training)
        x = F.relu(x)
        return F.sigmoid(self.lin3(x))


class D_net_gauss(nn.Module):
    def __init__(self):
        super(D_net_gauss, self).__init__()
        self.lin1 = nn.Linear(z_dim, N)
        self.lin2 = nn.Linear(N, N)
        self.lin3 = nn.Linear(N, 1)

    def forward(self, x):
        x = F.dropout(self.lin1(x), p=0.2, training=self.training)
        x = F.relu(x)
        x = F.dropout(self.lin2(x), p=0.2, training=self.training)
        x = F.relu(x)

        return F.sigmoid(self.lin3(x))


####################
# Utility functions
####################
def save_model(model, filename):
    print('Best model so far, saving it...')
    torch.save(model.state_dict(), filename)

def sample_categorical(batch_size, n_classes=10):
    '''
     Sample from a categorical distribution
     of size batch_size and # of classes n_classes
     return: torch.autograd.Variable with the sample
    '''
    cat = np.random.randint(0, 10, batch_size)
    cat = np.eye(n_classes)[cat].astype('float32')
    cat = torch.from_numpy(cat)
    return Variable(cat)


def report_loss(epoch, D_loss_cat, D_loss_gauss, G_loss, recon_loss):
    '''
    Print loss
    '''
    print('Epoch-{}; D_loss_cat: {:.4}: D_loss_gauss: {:.4}; G_loss: {:.4}; recon_loss: {:.4}'.format(epoch,
                                                                                                      D_loss_cat.data[0],
                                                                                                      D_loss_gauss.data[0],
                                                                                                      G_loss.data[0],
                                                                                                      recon_loss.data[0]))


def create_latent(Q, loader):
    '''
    Creates the latent representation for the samples in loader
    return:
        z_values: numpy array with the latent representations
        labels: the labels corresponding to the latent representations
    '''
    Q.eval()
    labels = []

    for batch_idx, (X, target) in enumerate(loader):

        X = X * 0.3081 + 0.1307
        # X.resize_(loader.batch_size, X_dim)
        X, target = Variable(X), Variable(target)
        labels.extend(target.data.tolist())
        if cuda:
            X, target = X.cuda(), target.cuda()
        # Reconstruction phase
        z_sample = Q(X)
        if batch_idx > 0:
            z_values = np.concatenate((z_values, np.array(z_sample.data.tolist())))
        else:
            z_values = np.array(z_sample.data.tolist())
    labels = np.array(labels)

    return z_values, labels


def classification_accuracy(Q, data_loader):
    Q.eval()
    labels = []
    test_loss = 0
    correct = 0

    for batch_idx, (X, target) in enumerate(data_loader):
        X = X * 0.3081 + 0.1307
        X.resize_(data_loader.batch_size, X_dim)
        X, target = Variable(X), Variable(target)
        if cuda:
            X, target = X.cuda(), target.cuda()

        labels.extend(target.data.tolist())
        # Reconstruction phase
        output = Q(X)[0]

        test_loss += F.nll_loss(output, target).data[0]

        pred = output.data.max(1)[1]
        correct += pred.eq(target.data).cpu().sum()

    test_loss /= len(data_loader)
    return 100. * correct / len(data_loader.dataset)

def newdataset():
    z_cat = np.arange(0, n_classes)
    z_cat = np.eye(n_classes)[z_cat].astype('float32')
    z_cat = torch.from_numpy(z_cat)
    z_cat = Variable(z_cat)
    if cuda:
        z_cat = z_cat.cuda()
    
    z_real_gauss = Variable(torch.randn(10, z_dim))
    if cuda:
        z_real_gauss = z_real_gauss.cuda()
    X_sample = P(torch.cat((z_cat, z_real_gauss),1))

    for i in range(5000):
        z_real_gauss = Variable(torch.randn(10, z_dim))
        if cuda:
            z_real_gauss = z_real_gauss.cuda()
        x_sample = P(torch.cat((z_cat, z_real_gauss),1))
        X_sample = torch.cat((x_sample, X_sample),0)

    xs = np.array(X_sample.data.tolist())

    pkl.dump(xs, open('./synthetic_samples', 'w'))

####################
# Train procedure
####################
def train(P, Q, D_cat, D_gauss, P_decoder, Q_encoder, Q_semi_supervised, Q_generator, D_cat_solver, D_gauss_solver, train_labeled_loader, train_unlabeled_loader):
    '''
    Train procedure for one epoch.
    '''
    TINY = 1e-15
    # Set the networks in train mode (apply dropout when needed)
    Q.train()
    P.train()
    D_cat.train()
    D_gauss.train()

    if train_unlabeled_loader is None:
        train_unlabeled_loader = train_labeled_loader

    # Loop through the labeled and unlabeled dataset getting one batch of samples from each
    # The batch size has to be a divisor of the size of the dataset or it will return
    # invalid samples
    for (X_l, target_l), (X_u, target_u) in itertools.izip(train_labeled_loader, train_unlabeled_loader):

        for X, target in [(X_u, target_u), (X_l, target_l)]:
            if target[0] == -1:
                labeled = False
            else:
                labeled = True

            # Load batch and normalize samples to be between 0 and 1
            X = X * 0.3081 + 0.1307
            X.resize_(train_batch_size, X_dim)

            X, target = Variable(X), Variable(target)
            if cuda:
                X, target = X.cuda(), target.cuda()

            # Init gradients
            P.zero_grad()
            Q.zero_grad()
            D_cat.zero_grad()
            D_gauss.zero_grad()

            #######################
            # Reconstruction phase
            #######################
            if not labeled:
                z_sample = torch.cat(Q(X), 1)
                X_sample = P(z_sample)

                recon_loss = F.binary_cross_entropy(X_sample + TINY, X.resize(train_batch_size, X_dim) + TINY)
                recon_loss = recon_loss
                recon_loss.backward()
                P_decoder.step()
                Q_encoder.step()

                P.zero_grad()
                Q.zero_grad()
                D_cat.zero_grad()
                D_gauss.zero_grad()
                recon_loss = recon_loss
                #######################
                # Regularization phase
                #######################
                # Discriminator
                Q.eval()
                z_real_cat = sample_categorical(train_batch_size, n_classes=n_classes)
                z_real_gauss = Variable(torch.randn(train_batch_size, z_dim))
                if cuda:
                    z_real_cat = z_real_cat.cuda()
                    z_real_gauss = z_real_gauss.cuda()

                z_fake_cat, z_fake_gauss = Q(X)

                D_real_cat = D_cat(z_real_cat)
                D_real_gauss = D_gauss(z_real_gauss)
                D_fake_cat = D_cat(z_fake_cat)
                D_fake_gauss = D_gauss(z_fake_gauss)

                D_loss_cat = -torch.mean(torch.log(D_real_cat + TINY) + torch.log(1 - D_fake_cat + TINY))
                D_loss_gauss = -torch.mean(torch.log(D_real_gauss + TINY) + torch.log(1 - D_fake_gauss + TINY))

                D_loss = D_loss_cat + D_loss_gauss
                D_loss = D_loss

                D_loss.backward()
                D_cat_solver.step()
                D_gauss_solver.step()

                P.zero_grad()
                Q.zero_grad()
                D_cat.zero_grad()
                D_gauss.zero_grad()

                # Generator
                Q.train()
                z_fake_cat, z_fake_gauss = Q(X)

                D_fake_cat = D_cat(z_fake_cat)
                D_fake_gauss = D_gauss(z_fake_gauss)

                G_loss = - torch.mean(torch.log(D_fake_cat + TINY)) - torch.mean(torch.log(D_fake_gauss + TINY))
                G_loss = G_loss
                G_loss.backward()
                Q_generator.step()

                P.zero_grad()
                Q.zero_grad()
                D_cat.zero_grad()
                D_gauss.zero_grad()

            #######################
            # Semi-supervised phase
            #######################
            if labeled:
                pred, _ = Q(X)
                class_loss = F.cross_entropy(pred, target)
                class_loss.backward()
                Q_semi_supervised.step()

                P.zero_grad()
                Q.zero_grad()
                D_cat.zero_grad()
                D_gauss.zero_grad()

    return D_loss_cat, D_loss_gauss, G_loss, recon_loss, class_loss


def generate_model(savefolder='./'):
    train_labeled_loader, train_unlabeled_loader, valid_loader = load_data()

    # Create and initialize Networks
    train_acc = []
    val_acc = []

    torch.manual_seed(10)

    if cuda:
        Q = Q_net().cuda()
        P = P_net().cuda()
        D_cat = D_net_cat().cuda()
        D_gauss = D_net_gauss().cuda()
    else:
        Q = Q_net()
        P = P_net()
        D_gauss = D_net_gauss()
        D_cat = D_net_cat()

    # Set learning rates
    gen_lr = 0.0006
    semi_lr = 0.001
    reg_lr = 0.0008

    # Set optimizators
    P_decoder = optim.Adam(P.parameters(), lr=gen_lr)
    Q_encoder = optim.Adam(Q.parameters(), lr=gen_lr)

    Q_semi_supervised = optim.Adam(Q.parameters(), lr=semi_lr)

    Q_generator = optim.Adam(Q.parameters(), lr=reg_lr)
    D_gauss_solver = optim.Adam(D_gauss.parameters(), lr=reg_lr)
    D_cat_solver = optim.Adam(D_cat.parameters(), lr=reg_lr)

    for epoch in range(epochs):
        if epoch == 75:
            P_decoder = optim.Adam(P.parameters(), lr=gen_lr / 10.)
            Q_encoder = optim.Adam(Q.parameters(), lr=gen_lr / 10.)

            Q_semi_supervised = optim.Adam(Q.parameters(), lr=semi_lr / 10.)

            Q_generator = optim.Adam(Q.parameters(), lr=reg_lr / 10.)
            D_gauss_solver = optim.Adam(D_gauss.parameters(), lr=reg_lr / 10.)
            D_cat_solver = optim.Adam(D_cat.parameters(), lr=reg_lr / 10.)

        D_loss_cat, D_loss_gauss, G_loss, recon_loss, class_loss = train(P, Q, D_cat,
                                                                         D_gauss, P_decoder,
                                                                         Q_encoder, Q_semi_supervised,
                                                                         Q_generator,
                                                                         D_cat_solver, D_gauss_solver,
                                                                         train_labeled_loader,
                                                                         train_unlabeled_loader)
        if epoch % 10 == 0:
            train_acc.append(classification_accuracy(Q, train_labeled_loader))
            val_acc.append(classification_accuracy(Q, valid_loader))
            report_loss(epoch, D_loss_cat, D_loss_gauss, G_loss, recon_loss)
            print('Classification Loss: {:.3}'.format(class_loss.data[0]))
            print('Validation accuracy: {} %'.format(val_acc[-1]))

    torch.save(Q.state_dict(), savefolder + 'Q_networkL')
    torch.save(P.state_dict(), savefolder + 'P_network')
    torch.save(D_cat.state_dict(), savefolder + 'D_cat_network')
    torch.save(D_gauss.state_dict(), savefolder + 'D_gauss_network')


if __name__ == '__main__':
    generate_model()







import argparse
import time
import torch
import pickle
import numpy as np
import itertools
from torch.autograd import Variable
import torch.nn as nn
import torch.nn.functional as F
import torch.autograd as autograd
import torch.optim as optim
from torch.nn.modules.upsampling import UpsamplingNearest2d

# Training settings
parser = argparse.ArgumentParser(description='PyTorch semi-supervised MNIST')

parser.add_argument('--batch-size', type=int, default=100, metavar='N',
                    help='input batch size for training (default: 100)')
parser.add_argument('--epochs', type=int, default=5000, metavar='N',
                    help='number of epochs to train (default: 10)')
parser.add_argument('--no-cuda', action='store_true', default=False,
                    help='enables CUDA training')


args = parser.parse_args()
args.cuda = not args.no_cuda and torch.cuda.is_available()

seed = 10
if args.cuda:
    torch.cuda.manual_seed(seed)
cuda = args.cuda

kwargs = {'num_workers': 1, 'pin_memory': True} if cuda else {}
n_classes = 10
z_dim = 10
X_dim = 784
y_dim = 10
convolutional = False
train_batch_size = args.batch_size
valid_batch_size = args.batch_size
N = 1000
epochs = args.epochs

##################################
# Load data and create Data loaders
##################################
def load_data(data_path='../data/'):
    print('loading data!')
    trainset_labeled = pickle.load(open(data_path + "train_labeled.p", "rb"))
    trainset_unlabeled = pickle.load(open(data_path + "train_unlabeled.p", "rb"))
    # Set -1 as labels for unlabeled data
    trainset_unlabeled.train_labels = torch.from_numpy(np.array([-1] * 47000))
    validset = pickle.load(open(data_path + "validation.p", "rb"))

    train_labeled_loader = torch.utils.data.DataLoader(trainset_labeled,
                                                       batch_size=train_batch_size,
                                                       shuffle=True, **kwargs)

    train_unlabeled_loader = torch.utils.data.DataLoader(trainset_unlabeled,
                                                         batch_size=train_batch_size,
                                                         shuffle=True, **kwargs)

    valid_loader = torch.utils.data.DataLoader(validset, batch_size=valid_batch_size, shuffle=True)

    return train_labeled_loader, train_unlabeled_loader, valid_loader

##################################
# Define Networks
##################################
# Encoder
class Q_net(nn.Module):
    def __init__(self):
        super(Q_net, self).__init__()
        self.lin1 = nn.Linear(X_dim, N)
        self.lin2 = nn.Linear(N, N)
        # Categorical code (y)
        self.lin3cat = nn.Linear(N, n_classes)
        # Gaussian code (z)
        self.lin3gauss = nn.Linear(N, z_dim)

    def forward(self, x):
        x = F.droppout(self.lin1(x), p=0.25, training=self.training)
        x = F.relu(x)
        x = F.droppout(self.lin2(x), p=0.25, training=self.training)
        x = F.relu(x)
        xcat = F.softmax(self.lin3cat(x))
        xgauss = self.lin3gauss(x)

        return xcat, xgauss

# Decoder
class P_net(nn.Module):
    def __init__(self):
        super(P_net, self).__init__()
        self.lin1 = nn.Linear(z_dim + n_classes, N)
        self.lin2 = nn.Linear(N, N)
        self.lin3 = nn.Linear(N, X_dim)

    def forward(self, x):
        x = self.lin1(x)
        x = F.dropout(x, p=0.25, training=self.training)
        x = F.relu(x)
        x = self.lin2(x)
        x = F.dropout(x, p=0.25, training=self.training)
        x = self.lin3(x)
        return F.sigmoid(x)


# Discriminator networks
class D_net_cat(nn.Module):
    def __init__(self):
        super(D_net_cat, self).__init__()
        self.lin1 = nn.Linear(n_classes, N)
        self.lin2 = nn.Linear(N, N)
        self.lin3 = nn.Linear(N, 1)

    def forward(self, x):
        x = F.dropout(self.lin1(x), p=0.2, training=self.training)
        x = F.relu(x)
        F.dropout(self.lin2(x), p=0.2, training=self.training)
        x = F.relu(x)
        return F.sigmoid(self.lin3(x))


class D_net_gauss(nn.Module):
    def __init__(self):
        super(D_net_gauss, self).__init__()
        self.lin1 = nn.Linear(z_dim, N)
        self.lin2 = nn.Linear(N, N)
        self.lin3 = nn.Linear(N, 1)

    def forward(self, x):
        x = F.dropout(self.lin1(x), p=0.2, training=self.training)
        x = F.relu(x)
        x = F.dropout(self.lin2(x), p=0.2, training=self.training)
        x = F.relu(x)

        return F.sigmoid(self.lin3(x))


####################
# Utility functions
####################
def save_model(model, filename):
    print('Best model so far, saving it...')
    torch.save(model.state_dict(), filename)

def sample_categorical(batch_size, n_classes=10):
    '''
     Sample from a categorical distribution
     of size batch_size and # of classes n_classes
     return: torch.autograd.Variable with the sample
    '''
    cat = np.random.randint(0, 10, batch_size)
    cat = np.eye(n_classes)[cat].astype('float32')
    cat = torch.from_numpy(cat)
    return Variable(cat)


def report_loss(epoch, D_loss_cat, D_loss_gauss, G_loss, recon_loss):
    '''
    Print loss
    '''
    print('Epoch-{}; D_loss_cat: {:.4}: D_loss_gauss: {:.4}; G_loss: {:.4}; recon_loss: {:.4}'.format(epoch,
                                                                                                      D_loss_cat.data[0],
                                                                                                      D_loss_gauss.data[0],
                                                                                                      G_loss.data[0],
                                                                                                      recon_loss.data[0]))


def create_latent(Q, loader):
    '''
    Creates the latent representation for the samples in loader
    return:
        z_values: numpy array with the latent representations
        labels: the labels corresponding to the latent representations
    '''
    Q.eval()
    labels = []

    for batch_idx, (X, target) in enumerate(loader):

        X = X * 0.3081 + 0.1307
        # X.resize_(loader.batch_size, X_dim)
        X, target = Variable(X), Variable(target)
        labels.extend(target.data.tolist())
        if cuda:
            X, target = X.cuda(), target.cuda()
        # Reconstruction phase
        z_sample = Q(X)
        if batch_idx > 0:
            z_values = np.concatenate((z_values, np.array(z_sample.data.tolist())))
        else:
            z_values = np.array(z_sample.data.tolist())
    labels = np.array(labels)

    return z_values, labels


def classification_accuracy(Q, data_loader):
    Q.eval()
    labels = []
    test_loss = 0
    correct = 0

    for batch_idx, (X, target) in enumerate(data_loader):
        X = X * 0.3081 + 0.1307
        X.resize_(data_loader.batch_size, X_dim)
        X, target = Variable(X), Variable(target)
        if cuda:
            X, target = X.cuda(), target.cuda()

        labels.extend(target.data.tolist())
        # Reconstruction phase
        output = Q(X)[0]

        test_loss += F.nll_loss(output, target).data[0]

        pred = output.data.max(1)[1]
        correct += pred.eq(target.data).cpu().sum()

    test_loss /= len(data_loader)
    return 100. * correct / len(data_loader.dataset)

def newdataset():
    z_cat = np.arange(0, n_classes)
    z_cat = np.eye(n_classes)[z_cat].astype('float32')
    z_cat = torch.from_numpy(z_cat)
    z_cat = Variable(z_cat)
    if cuda:
        z_cat = z_cat.cuda()
    
    z_real_gauss = Variable(torch.randn(10, z_dim))
    if cuda:
        z_real_gauss = z_real_gauss.cuda()
    X_sample = P(torch.cat((z_cat, z_real_gauss),1))

    for i in range(5000):
        z_real_gauss = Variable(torch.randn(10, z_dim))
        if cuda:
            z_real_gauss = z_real_gauss.cuda()
        x_sample = P(torch.cat((z_cat, z_real_gauss),1))
        X_sample = torch.cat((x_sample, X_sample),0)

    xs = np.array(X_sample.data.tolist())

    pkl.dump(xs, open('./synthetic_samples', 'w'))

####################
# Train procedure
####################
def train(P, Q, D_cat, D_gauss, P_decoder, Q_encoder, Q_semi_supervised, Q_generator, D_cat_solver, D_gauss_solver, train_labeled_loader, train_unlabeled_loader):
    '''
    Train procedure for one epoch.
    '''
    TINY = 1e-15
    # Set the networks in train mode (apply dropout when needed)
    Q.train()
    P.train()
    D_cat.train()
    D_gauss.train()

    if train_unlabeled_loader is None:
        train_unlabeled_loader = train_labeled_loader

    # Loop through the labeled and unlabeled dataset getting one batch of samples from each
    # The batch size has to be a divisor of the size of the dataset or it will return
    # invalid samples
    for (X_l, target_l), (X_u, target_u) in itertools.izip(train_labeled_loader, train_unlabeled_loader):

        for X, target in [(X_u, target_u), (X_l, target_l)]:
            if target[0] == -1:
                labeled = False
            else:
                labeled = True

            # Load batch and normalize samples to be between 0 and 1
            X = X * 0.3081 + 0.1307
            X.resize_(train_batch_size, X_dim)

            X, target = Variable(X), Variable(target)
            if cuda:
                X, target = X.cuda(), target.cuda()

            # Init gradients
            P.zero_grad()
            Q.zero_grad()
            D_cat.zero_grad()
            D_gauss.zero_grad()

            #######################
            # Reconstruction phase
            #######################
            if not labeled:
                z_sample = torch.cat(Q(X), 1)
                X_sample = P(z_sample)

                recon_loss = F.binary_cross_entropy(X_sample + TINY, X.resize(train_batch_size, X_dim) + TINY)
                recon_loss = recon_loss
                recon_loss.backward()
                P_decoder.step()
                Q_encoder.step()

                P.zero_grad()
                Q.zero_grad()
                D_cat.zero_grad()
                D_gauss.zero_grad()
                recon_loss = recon_loss
                #######################
                # Regularization phase
                #######################
                # Discriminator
                Q.eval()
                z_real_cat = sample_categorical(train_batch_size, n_classes=n_classes)
                z_real_gauss = Variable(torch.randn(train_batch_size, z_dim))
                if cuda:
                    z_real_cat = z_real_cat.cuda()
                    z_real_gauss = z_real_gauss.cuda()

                z_fake_cat, z_fake_gauss = Q(X)

                D_real_cat = D_cat(z_real_cat)
                D_real_gauss = D_gauss(z_real_gauss)
                D_fake_cat = D_cat(z_fake_cat)
                D_fake_gauss = D_gauss(z_fake_gauss)

                D_loss_cat = -torch.mean(torch.log(D_real_cat + TINY) + torch.log(1 - D_fake_cat + TINY))
                D_loss_gauss = -torch.mean(torch.log(D_real_gauss + TINY) + torch.log(1 - D_fake_gauss + TINY))

                D_loss = D_loss_cat + D_loss_gauss
                D_loss = D_loss

                D_loss.backward()
                D_cat_solver.step()
                D_gauss_solver.step()

                P.zero_grad()
                Q.zero_grad()
                D_cat.zero_grad()
                D_gauss.zero_grad()

                # Generator
                Q.train()
                z_fake_cat, z_fake_gauss = Q(X)

                D_fake_cat = D_cat(z_fake_cat)
                D_fake_gauss = D_gauss(z_fake_gauss)

                G_loss = - torch.mean(torch.log(D_fake_cat + TINY)) - torch.mean(torch.log(D_fake_gauss + TINY))
                G_loss = G_loss
                G_loss.backward()
                Q_generator.step()

                P.zero_grad()
                Q.zero_grad()
                D_cat.zero_grad()
                D_gauss.zero_grad()

            #######################
            # Semi-supervised phase
            #######################
            if labeled:
                pred, _ = Q(X)
                class_loss = F.cross_entropy(pred, target)
                class_loss.backward()
                Q_semi_supervised.step()

                P.zero_grad()
                Q.zero_grad()
                D_cat.zero_grad()
                D_gauss.zero_grad()

    return D_loss_cat, D_loss_gauss, G_loss, recon_loss, class_loss


def generate_model(savefolder='./'):
    train_labeled_loader, train_unlabeled_loader, valid_loader = load_data()

    # Create and initialize Networks
    train_acc = []
    val_acc = []

    torch.manual_seed(10)

    if cuda:
        Q = Q_net().cuda()
        P = P_net().cuda()
        D_cat = D_net_cat().cuda()
        D_gauss = D_net_gauss().cuda()
    else:
        Q = Q_net()
        P = P_net()
        D_gauss = D_net_gauss()
        D_cat = D_net_cat()

    # Set learning rates
    gen_lr = 0.0006
    semi_lr = 0.001
    reg_lr = 0.0008

    # Set optimizators
    P_decoder = optim.Adam(P.parameters(), lr=gen_lr)
    Q_encoder = optim.Adam(Q.parameters(), lr=gen_lr)

    Q_semi_supervised = optim.Adam(Q.parameters(), lr=semi_lr)

    Q_generator = optim.Adam(Q.parameters(), lr=reg_lr)
    D_gauss_solver = optim.Adam(D_gauss.parameters(), lr=reg_lr)
    D_cat_solver = optim.Adam(D_cat.parameters(), lr=reg_lr)

    for epoch in range(epochs):
        if epoch == 75:
            P_decoder = optim.Adam(P.parameters(), lr=gen_lr / 10.)
            Q_encoder = optim.Adam(Q.parameters(), lr=gen_lr / 10.)

            Q_semi_supervised = optim.Adam(Q.parameters(), lr=semi_lr / 10.)

            Q_generator = optim.Adam(Q.parameters(), lr=reg_lr / 10.)
            D_gauss_solver = optim.Adam(D_gauss.parameters(), lr=reg_lr / 10.)
            D_cat_solver = optim.Adam(D_cat.parameters(), lr=reg_lr / 10.)

        D_loss_cat, D_loss_gauss, G_loss, recon_loss, class_loss = train(P, Q, D_cat,
                                                                         D_gauss, P_decoder,
                                                                         Q_encoder, Q_semi_supervised,
                                                                         Q_generator,
                                                                         D_cat_solver, D_gauss_solver,
                                                                         train_labeled_loader,
                                                                         train_unlabeled_loader)
        if epoch % 10 == 0:
            train_acc.append(classification_accuracy(Q, train_labeled_loader))
            val_acc.append(classification_accuracy(Q, valid_loader))
            report_loss(epoch, D_loss_cat, D_loss_gauss, G_loss, recon_loss)
            print('Classification Loss: {:.3}'.format(class_loss.data[0]))
            print('Validation accuracy: {} %'.format(val_acc[-1]))

    torch.save(Q.state_dict(), savefolder + 'Q_networkL')
    torch.save(P.state_dict(), savefolder + 'P_network')
    torch.save(D_cat.state_dict(), savefolder + 'D_cat_network')
    torch.save(D_gauss.state_dict(), savefolder + 'D_gauss_network')


if __name__ == '__main__':
    generate_model()







import argparse
import time
import torch
import pickle
import numpy as np
import itertools
from torch.autograd import Variable
import torch.nn as nn
import torch.nn.functional as F
import torch.autograd as autograd
import torch.optim as optim
from torch.nn.modules.upsampling import UpsamplingNearest2d

# Training settings
parser = argparse.ArgumentParser(description='PyTorch semi-supervised MNIST')

parser.add_argument('--batch-size', type=int, default=100, metavar='N',
                    help='input batch size for training (default: 100)')
parser.add_argument('--epochs', type=int, default=5000, metavar='N',
                    help='number of epochs to train (default: 10)')
parser.add_argument('--no-cuda', action='store_true', default=False,
                    help='enables CUDA training')


args = parser.parse_args()
args.cuda = not args.no_cuda and torch.cuda.is_available()

seed = 10
if args.cuda:
    torch.cuda.manual_seed(seed)
cuda = args.cuda

kwargs = {'num_workers': 1, 'pin_memory': True} if cuda else {}
n_classes = 10
z_dim = 10
X_dim = 784
y_dim = 10
convolutional = False
train_batch_size = args.batch_size
valid_batch_size = args.batch_size
N = 1000
epochs = args.epochs

##################################
# Load data and create Data loaders
##################################
def load_data(data_path='../data/'):
    print('loading data!')
    trainset_labeled = pickle.load(open(data_path + "train_labeled.p", "rb"))
    trainset_unlabeled = pickle.load(open(data_path + "train_unlabeled.p", "rb"))
    # Set -1 as labels for unlabeled data
    trainset_unlabeled.train_labels = torch.from_numpy(np.array([-1] * 47000))
    validset = pickle.load(open(data_path + "validation.p", "rb"))

    train_labeled_loader = torch.utils.data.DataLoader(trainset_labeled,
                                                       batch_size=train_batch_size,
                                                       shuffle=True, **kwargs)

    train_unlabeled_loader = torch.utils.data.DataLoader(trainset_unlabeled,
                                                         batch_size=train_batch_size,
                                                         shuffle=True, **kwargs)

    valid_loader = torch.utils.data.DataLoader(validset, batch_size=valid_batch_size, shuffle=True)

    return train_labeled_loader, train_unlabeled_loader, valid_loader

##################################
# Define Networks
##################################
# Encoder
class Q_net(nn.Module):
    def __init__(self):
        super(Q_net, self).__init__()
        self.lin1 = nn.Linear(X_dim, N)
        self.lin2 = nn.Linear(N, N)
        # Categorical code (y)
        self.lin3cat = nn.Linear(N, n_classes)
        # Gaussian code (z)
        self.lin3gauss = nn.Linear(N, z_dim)

    def forward(self, x):
        x = F.droppout(self.lin1(x), p=0.25, training=self.training)
        x = F.relu(x)
        x = F.droppout(self.lin2(x), p=0.25, training=self.training)
        x = F.relu(x)
        xcat = F.softmax(self.lin3cat(x))
        xgauss = self.lin3gauss(x)

        return xcat, xgauss

# Decoder
class P_net(nn.Module):
    def __init__(self):
        super(P_net, self).__init__()
        self.lin1 = nn.Linear(z_dim + n_classes, N)
        self.lin2 = nn.Linear(N, N)
        self.lin3 = nn.Linear(N, X_dim)

    def forward(self, x):
        x = self.lin1(x)
        x = F.dropout(x, p=0.25, training=self.training)
        x = F.relu(x)
        x = self.lin2(x)
        x = F.dropout(x, p=0.25, training=self.training)
        x = self.lin3(x)
        return F.sigmoid(x)


# Discriminator networks
class D_net_cat(nn.Module):
    def __init__(self):
        super(D_net_cat, self).__init__()
        self.lin1 = nn.Linear(n_classes, N)
        self.lin2 = nn.Linear(N, N)
        self.lin3 = nn.Linear(N, 1)

    def forward(self, x):
        x = F.dropout(self.lin1(x), p=0.2, training=self.training)
        x = F.relu(x)
        F.dropout(self.lin2(x), p=0.2, training=self.training)
        x = F.relu(x)
        return F.sigmoid(self.lin3(x))


class D_net_gauss(nn.Module):
    def __init__(self):
        super(D_net_gauss, self).__init__()
        self.lin1 = nn.Linear(z_dim, N)
        self.lin2 = nn.Linear(N, N)
        self.lin3 = nn.Linear(N, 1)

    def forward(self, x):
        x = F.dropout(self.lin1(x), p=0.2, training=self.training)
        x = F.relu(x)
        x = F.dropout(self.lin2(x), p=0.2, training=self.training)
        x = F.relu(x)

        return F.sigmoid(self.lin3(x))


####################
# Utility functions
####################
def save_model(model, filename):
    print('Best model so far, saving it...')
    torch.save(model.state_dict(), filename)

def sample_categorical(batch_size, n_classes=10):
    '''
     Sample from a categorical distribution
     of size batch_size and # of classes n_classes
     return: torch.autograd.Variable with the sample
    '''
    cat = np.random.randint(0, 10, batch_size)
    cat = np.eye(n_classes)[cat].astype('float32')
    cat = torch.from_numpy(cat)
    return Variable(cat)


def report_loss(epoch, D_loss_cat, D_loss_gauss, G_loss, recon_loss):
    '''
    Print loss
    '''
    print('Epoch-{}; D_loss_cat: {:.4}: D_loss_gauss: {:.4}; G_loss: {:.4}; recon_loss: {:.4}'.format(epoch,
                                                                                                      D_loss_cat.data[0],
                                                                                                      D_loss_gauss.data[0],
                                                                                                      G_loss.data[0],
                                                                                                      recon_loss.data[0]))


def create_latent(Q, loader):
    '''
    Creates the latent representation for the samples in loader
    return:
        z_values: numpy array with the latent representations
        labels: the labels corresponding to the latent representations
    '''
    Q.eval()
    labels = []

    for batch_idx, (X, target) in enumerate(loader):

        X = X * 0.3081 + 0.1307
        # X.resize_(loader.batch_size, X_dim)
        X, target = Variable(X), Variable(target)
        labels.extend(target.data.tolist())
        if cuda:
            X, target = X.cuda(), target.cuda()
        # Reconstruction phase
        z_sample = Q(X)
        if batch_idx > 0:
            z_values = np.concatenate((z_values, np.array(z_sample.data.tolist())))
        else:
            z_values = np.array(z_sample.data.tolist())
    labels = np.array(labels)

    return z_values, labels


def classification_accuracy(Q, data_loader):
    Q.eval()
    labels = []
    test_loss = 0
    correct = 0

    for batch_idx, (X, target) in enumerate(data_loader):
        X = X * 0.3081 + 0.1307
        X.resize_(data_loader.batch_size, X_dim)
        X, target = Variable(X), Variable(target)
        if cuda:
            X, target = X.cuda(), target.cuda()

        labels.extend(target.data.tolist())
        # Reconstruction phase
        output = Q(X)[0]

        test_loss += F.nll_loss(output, target).data[0]

        pred = output.data.max(1)[1]
        correct += pred.eq(target.data).cpu().sum()

    test_loss /= len(data_loader)
    return 100. * correct / len(data_loader.dataset)

def newdataset():
    z_cat = np.arange(0, n_classes)
    z_cat = np.eye(n_classes)[z_cat].astype('float32')
    z_cat = torch.from_numpy(z_cat)
    z_cat = Variable(z_cat)
    if cuda:
        z_cat = z_cat.cuda()
    
    z_real_gauss = Variable(torch.randn(10, z_dim))
    if cuda:
        z_real_gauss = z_real_gauss.cuda()
    X_sample = P(torch.cat((z_cat, z_real_gauss),1))

    for i in range(5000):
        z_real_gauss = Variable(torch.randn(10, z_dim))
        if cuda:
            z_real_gauss = z_real_gauss.cuda()
        x_sample = P(torch.cat((z_cat, z_real_gauss),1))
        X_sample = torch.cat((x_sample, X_sample),0)

    xs = np.array(X_sample.data.tolist())

    pkl.dump(xs, open('./synthetic_samples', 'w'))

####################
# Train procedure
####################
def train(P, Q, D_cat, D_gauss, P_decoder, Q_encoder, Q_semi_supervised, Q_generator, D_cat_solver, D_gauss_solver, train_labeled_loader, train_unlabeled_loader):
    '''
    Train procedure for one epoch.
    '''
    TINY = 1e-15
    # Set the networks in train mode (apply dropout when needed)
    Q.train()
    P.train()
    D_cat.train()
    D_gauss.train()

    if train_unlabeled_loader is None:
        train_unlabeled_loader = train_labeled_loader

    # Loop through the labeled and unlabeled dataset getting one batch of samples from each
    # The batch size has to be a divisor of the size of the dataset or it will return
    # invalid samples
    for (X_l, target_l), (X_u, target_u) in itertools.izip(train_labeled_loader, train_unlabeled_loader):

        for X, target in [(X_u, target_u), (X_l, target_l)]:
            if target[0] == -1:
                labeled = False
            else:
                labeled = True

            # Load batch and normalize samples to be between 0 and 1
            X = X * 0.3081 + 0.1307
            X.resize_(train_batch_size, X_dim)

            X, target = Variable(X), Variable(target)
            if cuda:
                X, target = X.cuda(), target.cuda()

            # Init gradients
            P.zero_grad()
            Q.zero_grad()
            D_cat.zero_grad()
            D_gauss.zero_grad()

            #######################
            # Reconstruction phase
            #######################
            if not labeled:
                z_sample = torch.cat(Q(X), 1)
                X_sample = P(z_sample)

                recon_loss = F.binary_cross_entropy(X_sample + TINY, X.resize(train_batch_size, X_dim) + TINY)
                recon_loss = recon_loss
                recon_loss.backward()
                P_decoder.step()
                Q_encoder.step()

                P.zero_grad()
                Q.zero_grad()
                D_cat.zero_grad()
                D_gauss.zero_grad()
                recon_loss = recon_loss
                #######################
                # Regularization phase
                #######################
                # Discriminator
                Q.eval()
                z_real_cat = sample_categorical(train_batch_size, n_classes=n_classes)
                z_real_gauss = Variable(torch.randn(train_batch_size, z_dim))
                if cuda:
                    z_real_cat = z_real_cat.cuda()
                    z_real_gauss = z_real_gauss.cuda()

                z_fake_cat, z_fake_gauss = Q(X)

                D_real_cat = D_cat(z_real_cat)
                D_real_gauss = D_gauss(z_real_gauss)
                D_fake_cat = D_cat(z_fake_cat)
                D_fake_gauss = D_gauss(z_fake_gauss)

                D_loss_cat = -torch.mean(torch.log(D_real_cat + TINY) + torch.log(1 - D_fake_cat + TINY))
                D_loss_gauss = -torch.mean(torch.log(D_real_gauss + TINY) + torch.log(1 - D_fake_gauss + TINY))

                D_loss = D_loss_cat + D_loss_gauss
                D_loss = D_loss

                D_loss.backward()
                D_cat_solver.step()
                D_gauss_solver.step()

                P.zero_grad()
                Q.zero_grad()
                D_cat.zero_grad()
                D_gauss.zero_grad()

                # Generator
                Q.train()
                z_fake_cat, z_fake_gauss = Q(X)

                D_fake_cat = D_cat(z_fake_cat)
                D_fake_gauss = D_gauss(z_fake_gauss)

                G_loss = - torch.mean(torch.log(D_fake_cat + TINY)) - torch.mean(torch.log(D_fake_gauss + TINY))
                G_loss = G_loss
                G_loss.backward()
                Q_generator.step()

                P.zero_grad()
                Q.zero_grad()
                D_cat.zero_grad()
                D_gauss.zero_grad()

            #######################
            # Semi-supervised phase
            #######################
            if labeled:
                pred, _ = Q(X)
                class_loss = F.cross_entropy(pred, target)
                class_loss.backward()
                Q_semi_supervised.step()

                P.zero_grad()
                Q.zero_grad()
                D_cat.zero_grad()
                D_gauss.zero_grad()

    return D_loss_cat, D_loss_gauss, G_loss, recon_loss, class_loss


def generate_model(savefolder='./'):
    train_labeled_loader, train_unlabeled_loader, valid_loader = load_data()

    # Create and initialize Networks
    train_acc = []
    val_acc = []

    torch.manual_seed(10)

    if cuda:
        Q = Q_net().cuda()
        P = P_net().cuda()
        D_cat = D_net_cat().cuda()
        D_gauss = D_net_gauss().cuda()
    else:
        Q = Q_net()
        P = P_net()
        D_gauss = D_net_gauss()
        D_cat = D_net_cat()

    # Set learning rates
    gen_lr = 0.0006
    semi_lr = 0.001
    reg_lr = 0.0008

    # Set optimizators
    P_decoder = optim.Adam(P.parameters(), lr=gen_lr)
    Q_encoder = optim.Adam(Q.parameters(), lr=gen_lr)

    Q_semi_supervised = optim.Adam(Q.parameters(), lr=semi_lr)

    Q_generator = optim.Adam(Q.parameters(), lr=reg_lr)
    D_gauss_solver = optim.Adam(D_gauss.parameters(), lr=reg_lr)
    D_cat_solver = optim.Adam(D_cat.parameters(), lr=reg_lr)

    for epoch in range(epochs):
        if epoch == 75:
            P_decoder = optim.Adam(P.parameters(), lr=gen_lr / 10.)
            Q_encoder = optim.Adam(Q.parameters(), lr=gen_lr / 10.)

            Q_semi_supervised = optim.Adam(Q.parameters(), lr=semi_lr / 10.)

            Q_generator = optim.Adam(Q.parameters(), lr=reg_lr / 10.)
            D_gauss_solver = optim.Adam(D_gauss.parameters(), lr=reg_lr / 10.)
            D_cat_solver = optim.Adam(D_cat.parameters(), lr=reg_lr / 10.)

        D_loss_cat, D_loss_gauss, G_loss, recon_loss, class_loss = train(P, Q, D_cat,
                                                                         D_gauss, P_decoder,
                                                                         Q_encoder, Q_semi_supervised,
                                                                         Q_generator,
                                                                         D_cat_solver, D_gauss_solver,
                                                                         train_labeled_loader,
                                                                         train_unlabeled_loader)
        if epoch % 10 == 0:
            train_acc.append(classification_accuracy(Q, train_labeled_loader))
            val_acc.append(classification_accuracy(Q, valid_loader))
            report_loss(epoch, D_loss_cat, D_loss_gauss, G_loss, recon_loss)
            print('Classification Loss: {:.3}'.format(class_loss.data[0]))
            print('Validation accuracy: {} %'.format(val_acc[-1]))

    torch.save(Q.state_dict(), savefolder + 'Q_networkL')
    torch.save(P.state_dict(), savefolder + 'P_network')
    torch.save(D_cat.state_dict(), savefolder + 'D_cat_network')
    torch.save(D_gauss.state_dict(), savefolder + 'D_gauss_network')


if __name__ == '__main__':
    generate_model()







import argparse
import time
import torch
import pickle
import numpy as np
import itertools
from torch.autograd import Variable
import torch.nn as nn
import torch.nn.functional as F
import torch.autograd as autograd
import torch.optim as optim
from torch.nn.modules.upsampling import UpsamplingNearest2d

# Training settings
parser = argparse.ArgumentParser(description='PyTorch semi-supervised MNIST')

parser.add_argument('--batch-size', type=int, default=100, metavar='N',
                    help='input batch size for training (default: 100)')
parser.add_argument('--epochs', type=int, default=5000, metavar='N',
                    help='number of epochs to train (default: 10)')
parser.add_argument('--no-cuda', action='store_true', default=False,
                    help='enables CUDA training')


args = parser.parse_args()
args.cuda = not args.no_cuda and torch.cuda.is_available()

seed = 10
if args.cuda:
    torch.cuda.manual_seed(seed)
cuda = args.cuda

kwargs = {'num_workers': 1, 'pin_memory': True} if cuda else {}
n_classes = 10
z_dim = 10
X_dim = 784
y_dim = 10
convolutional = False
train_batch_size = args.batch_size
valid_batch_size = args.batch_size
N = 1000
epochs = args.epochs

##################################
# Load data and create Data loaders
##################################
def load_data(data_path='../data/'):
    print('loading data!')
    trainset_labeled = pickle.load(open(data_path + "train_labeled.p", "rb"))
    trainset_unlabeled = pickle.load(open(data_path + "train_unlabeled.p", "rb"))
    # Set -1 as labels for unlabeled data
    trainset_unlabeled.train_labels = torch.from_numpy(np.array([-1] * 47000))
    validset = pickle.load(open(data_path + "validation.p", "rb"))

    train_labeled_loader = torch.utils.data.DataLoader(trainset_labeled,
                                                       batch_size=train_batch_size,
                                                       shuffle=True, **kwargs)

    train_unlabeled_loader = torch.utils.data.DataLoader(trainset_unlabeled,
                                                         batch_size=train_batch_size,
                                                         shuffle=True, **kwargs)

    valid_loader = torch.utils.data.DataLoader(validset, batch_size=valid_batch_size, shuffle=True)

    return train_labeled_loader, train_unlabeled_loader, valid_loader

##################################
# Define Networks
##################################
# Encoder
class Q_net(nn.Module):
    def __init__(self):
        super(Q_net, self).__init__()
        self.lin1 = nn.Linear(X_dim, N)
        self.lin2 = nn.Linear(N, N)
        # Categorical code (y)
        self.lin3cat = nn.Linear(N, n_classes)
        # Gaussian code (z)
        self.lin3gauss = nn.Linear(N, z_dim)

    def forward(self, x):
        x = F.droppout(self.lin1(x), p=0.25, training=self.training)
        x = F.relu(x)
        x = F.droppout(self.lin2(x), p=0.25, training=self.training)
        x = F.relu(x)
        xcat = F.softmax(self.lin3cat(x))
        xgauss = self.lin3gauss(x)

        return xcat, xgauss

# Decoder
class P_net(nn.Module):
    def __init__(self):
        super(P_net, self).__init__()
        self.lin1 = nn.Linear(z_dim + n_classes, N)
        self.lin2 = nn.Linear(N, N)
        self.lin3 = nn.Linear(N, X_dim)

    def forward(self, x):
        x = self.lin1(x)
        x = F.dropout(x, p=0.25, training=self.training)
        x = F.relu(x)
        x = self.lin2(x)
        x = F.dropout(x, p=0.25, training=self.training)
        x = self.lin3(x)
        return F.sigmoid(x)


# Discriminator networks
class D_net_cat(nn.Module):
    def __init__(self):
        super(D_net_cat, self).__init__()
        self.lin1 = nn.Linear(n_classes, N)
        self.lin2 = nn.Linear(N, N)
        self.lin3 = nn.Linear(N, 1)

    def forward(self, x):
        x = F.dropout(self.lin1(x), p=0.2, training=self.training)
        x = F.relu(x)
        F.dropout(self.lin2(x), p=0.2, training=self.training)
        x = F.relu(x)
        return F.sigmoid(self.lin3(x))


class D_net_gauss(nn.Module):
    def __init__(self):
        super(D_net_gauss, self).__init__()
        self.lin1 = nn.Linear(z_dim, N)
        self.lin2 = nn.Linear(N, N)
        self.lin3 = nn.Linear(N, 1)

    def forward(self, x):
        x = F.dropout(self.lin1(x), p=0.2, training=self.training)
        x = F.relu(x)
        x = F.dropout(self.lin2(x), p=0.2, training=self.training)
        x = F.relu(x)

        return F.sigmoid(self.lin3(x))


####################
# Utility functions
####################
def save_model(model, filename):
    print('Best model so far, saving it...')
    torch.save(model.state_dict(), filename)

def sample_categorical(batch_size, n_classes=10):
    '''
     Sample from a categorical distribution
     of size batch_size and # of classes n_classes
     return: torch.autograd.Variable with the sample
    '''
    cat = np.random.randint(0, 10, batch_size)
    cat = np.eye(n_classes)[cat].astype('float32')
    cat = torch.from_numpy(cat)
    return Variable(cat)


def report_loss(epoch, D_loss_cat, D_loss_gauss, G_loss, recon_loss):
    '''
    Print loss
    '''
    print('Epoch-{}; D_loss_cat: {:.4}: D_loss_gauss: {:.4}; G_loss: {:.4}; recon_loss: {:.4}'.format(epoch,
                                                                                                      D_loss_cat.data[0],
                                                                                                      D_loss_gauss.data[0],
                                                                                                      G_loss.data[0],
                                                                                                      recon_loss.data[0]))


def create_latent(Q, loader):
    '''
    Creates the latent representation for the samples in loader
    return:
        z_values: numpy array with the latent representations
        labels: the labels corresponding to the latent representations
    '''
    Q.eval()
    labels = []

    for batch_idx, (X, target) in enumerate(loader):

        X = X * 0.3081 + 0.1307
        # X.resize_(loader.batch_size, X_dim)
        X, target = Variable(X), Variable(target)
        labels.extend(target.data.tolist())
        if cuda:
            X, target = X.cuda(), target.cuda()
        # Reconstruction phase
        z_sample = Q(X)
        if batch_idx > 0:
            z_values = np.concatenate((z_values, np.array(z_sample.data.tolist())))
        else:
            z_values = np.array(z_sample.data.tolist())
    labels = np.array(labels)

    return z_values, labels


def classification_accuracy(Q, data_loader):
    Q.eval()
    labels = []
    test_loss = 0
    correct = 0

    for batch_idx, (X, target) in enumerate(data_loader):
        X = X * 0.3081 + 0.1307
        X.resize_(data_loader.batch_size, X_dim)
        X, target = Variable(X), Variable(target)
        if cuda:
            X, target = X.cuda(), target.cuda()

        labels.extend(target.data.tolist())
        # Reconstruction phase
        output = Q(X)[0]

        test_loss += F.nll_loss(output, target).data[0]

        pred = output.data.max(1)[1]
        correct += pred.eq(target.data).cpu().sum()

    test_loss /= len(data_loader)
    return 100. * correct / len(data_loader.dataset)

def newdataset():
    z_cat = np.arange(0, n_classes)
    z_cat = np.eye(n_classes)[z_cat].astype('float32')
    z_cat = torch.from_numpy(z_cat)
    z_cat = Variable(z_cat)
    if cuda:
        z_cat = z_cat.cuda()
    
    z_real_gauss = Variable(torch.randn(10, z_dim))
    if cuda:
        z_real_gauss = z_real_gauss.cuda()
    X_sample = P(torch.cat((z_cat, z_real_gauss),1))

    for i in range(5000):
        z_real_gauss = Variable(torch.randn(10, z_dim))
        if cuda:
            z_real_gauss = z_real_gauss.cuda()
        x_sample = P(torch.cat((z_cat, z_real_gauss),1))
        X_sample = torch.cat((x_sample, X_sample),0)

    xs = np.array(X_sample.data.tolist())

    pkl.dump(xs, open('./synthetic_samples', 'w'))

####################
# Train procedure
####################
def train(P, Q, D_cat, D_gauss, P_decoder, Q_encoder, Q_semi_supervised, Q_generator, D_cat_solver, D_gauss_solver, train_labeled_loader, train_unlabeled_loader):
    '''
    Train procedure for one epoch.
    '''
    TINY = 1e-15
    # Set the networks in train mode (apply dropout when needed)
    Q.train()
    P.train()
    D_cat.train()
    D_gauss.train()

    if train_unlabeled_loader is None:
        train_unlabeled_loader = train_labeled_loader

    # Loop through the labeled and unlabeled dataset getting one batch of samples from each
    # The batch size has to be a divisor of the size of the dataset or it will return
    # invalid samples
    for (X_l, target_l), (X_u, target_u) in itertools.izip(train_labeled_loader, train_unlabeled_loader):

        for X, target in [(X_u, target_u), (X_l, target_l)]:
            if target[0] == -1:
                labeled = False
            else:
                labeled = True

            # Load batch and normalize samples to be between 0 and 1
            X = X * 0.3081 + 0.1307
            X.resize_(train_batch_size, X_dim)

            X, target = Variable(X), Variable(target)
            if cuda:
                X, target = X.cuda(), target.cuda()

            # Init gradients
            P.zero_grad()
            Q.zero_grad()
            D_cat.zero_grad()
            D_gauss.zero_grad()

            #######################
            # Reconstruction phase
            #######################
            if not labeled:
                z_sample = torch.cat(Q(X), 1)
                X_sample = P(z_sample)

                recon_loss = F.binary_cross_entropy(X_sample + TINY, X.resize(train_batch_size, X_dim) + TINY)
                recon_loss = recon_loss
                recon_loss.backward()
                P_decoder.step()
                Q_encoder.step()

                P.zero_grad()
                Q.zero_grad()
                D_cat.zero_grad()
                D_gauss.zero_grad()
                recon_loss = recon_loss
                #######################
                # Regularization phase
                #######################
                # Discriminator
                Q.eval()
                z_real_cat = sample_categorical(train_batch_size, n_classes=n_classes)
                z_real_gauss = Variable(torch.randn(train_batch_size, z_dim))
                if cuda:
                    z_real_cat = z_real_cat.cuda()
                    z_real_gauss = z_real_gauss.cuda()

                z_fake_cat, z_fake_gauss = Q(X)

                D_real_cat = D_cat(z_real_cat)
                D_real_gauss = D_gauss(z_real_gauss)
                D_fake_cat = D_cat(z_fake_cat)
                D_fake_gauss = D_gauss(z_fake_gauss)

                D_loss_cat = -torch.mean(torch.log(D_real_cat + TINY) + torch.log(1 - D_fake_cat + TINY))
                D_loss_gauss = -torch.mean(torch.log(D_real_gauss + TINY) + torch.log(1 - D_fake_gauss + TINY))

                D_loss = D_loss_cat + D_loss_gauss
                D_loss = D_loss

                D_loss.backward()
                D_cat_solver.step()
                D_gauss_solver.step()

                P.zero_grad()
                Q.zero_grad()
                D_cat.zero_grad()
                D_gauss.zero_grad()

                # Generator
                Q.train()
                z_fake_cat, z_fake_gauss = Q(X)

                D_fake_cat = D_cat(z_fake_cat)
                D_fake_gauss = D_gauss(z_fake_gauss)

                G_loss = - torch.mean(torch.log(D_fake_cat + TINY)) - torch.mean(torch.log(D_fake_gauss + TINY))
                G_loss = G_loss
                G_loss.backward()
                Q_generator.step()

                P.zero_grad()
                Q.zero_grad()
                D_cat.zero_grad()
                D_gauss.zero_grad()

            #######################
            # Semi-supervised phase
            #######################
            if labeled:
                pred, _ = Q(X)
                class_loss = F.cross_entropy(pred, target)
                class_loss.backward()
                Q_semi_supervised.step()

                P.zero_grad()
                Q.zero_grad()
                D_cat.zero_grad()
                D_gauss.zero_grad()

    return D_loss_cat, D_loss_gauss, G_loss, recon_loss, class_loss


def generate_model(savefolder='./'):
    train_labeled_loader, train_unlabeled_loader, valid_loader = load_data()

    # Create and initialize Networks
    train_acc = []
    val_acc = []

    torch.manual_seed(10)

    if cuda:
        Q = Q_net().cuda()
        P = P_net().cuda()
        D_cat = D_net_cat().cuda()
        D_gauss = D_net_gauss().cuda()
    else:
        Q = Q_net()
        P = P_net()
        D_gauss = D_net_gauss()
        D_cat = D_net_cat()

    # Set learning rates
    gen_lr = 0.0006
    semi_lr = 0.001
    reg_lr = 0.0008

    # Set optimizators
    P_decoder = optim.Adam(P.parameters(), lr=gen_lr)
    Q_encoder = optim.Adam(Q.parameters(), lr=gen_lr)

    Q_semi_supervised = optim.Adam(Q.parameters(), lr=semi_lr)

    Q_generator = optim.Adam(Q.parameters(), lr=reg_lr)
    D_gauss_solver = optim.Adam(D_gauss.parameters(), lr=reg_lr)
    D_cat_solver = optim.Adam(D_cat.parameters(), lr=reg_lr)

    for epoch in range(epochs):
        if epoch == 75:
            P_decoder = optim.Adam(P.parameters(), lr=gen_lr / 10.)
            Q_encoder = optim.Adam(Q.parameters(), lr=gen_lr / 10.)

            Q_semi_supervised = optim.Adam(Q.parameters(), lr=semi_lr / 10.)

            Q_generator = optim.Adam(Q.parameters(), lr=reg_lr / 10.)
            D_gauss_solver = optim.Adam(D_gauss.parameters(), lr=reg_lr / 10.)
            D_cat_solver = optim.Adam(D_cat.parameters(), lr=reg_lr / 10.)

        D_loss_cat, D_loss_gauss, G_loss, recon_loss, class_loss = train(P, Q, D_cat,
                                                                         D_gauss, P_decoder,
                                                                         Q_encoder, Q_semi_supervised,
                                                                         Q_generator,
                                                                         D_cat_solver, D_gauss_solver,
                                                                         train_labeled_loader,
                                                                         train_unlabeled_loader)
        if epoch % 10 == 0:
            train_acc.append(classification_accuracy(Q, train_labeled_loader))
            val_acc.append(classification_accuracy(Q, valid_loader))
            report_loss(epoch, D_loss_cat, D_loss_gauss, G_loss, recon_loss)
            print('Classification Loss: {:.3}'.format(class_loss.data[0]))
            print('Validation accuracy: {} %'.format(val_acc[-1]))

    torch.save(Q.state_dict(), savefolder + 'Q_networkL')
    torch.save(P.state_dict(), savefolder + 'P_network')
    torch.save(D_cat.state_dict(), savefolder + 'D_cat_network')
    torch.save(D_gauss.state_dict(), savefolder + 'D_gauss_network')


if __name__ == '__main__':
    generate_model()







import argparse
import time
import torch
import pickle
import numpy as np
import itertools
from torch.autograd import Variable
import torch.nn as nn
import torch.nn.functional as F
import torch.autograd as autograd
import torch.optim as optim
from torch.nn.modules.upsampling import UpsamplingNearest2d

# Training settings
parser = argparse.ArgumentParser(description='PyTorch semi-supervised MNIST')

parser.add_argument('--batch-size', type=int, default=100, metavar='N',
                    help='input batch size for training (default: 100)')
parser.add_argument('--epochs', type=int, default=5000, metavar='N',
                    help='number of epochs to train (default: 10)')
parser.add_argument('--no-cuda', action='store_true', default=False,
                    help='enables CUDA training')


args = parser.parse_args()
args.cuda = not args.no_cuda and torch.cuda.is_available()

seed = 10
if args.cuda:
    torch.cuda.manual_seed(seed)
cuda = args.cuda

kwargs = {'num_workers': 1, 'pin_memory': True} if cuda else {}
n_classes = 10
z_dim = 10
X_dim = 784
y_dim = 10
convolutional = False
train_batch_size = args.batch_size
valid_batch_size = args.batch_size
N = 1000
epochs = args.epochs

##################################
# Load data and create Data loaders
##################################
def load_data(data_path='../data/'):
    print('loading data!')
    trainset_labeled = pickle.load(open(data_path + "train_labeled.p", "rb"))
    trainset_unlabeled = pickle.load(open(data_path + "train_unlabeled.p", "rb"))
    # Set -1 as labels for unlabeled data
    trainset_unlabeled.train_labels = torch.from_numpy(np.array([-1] * 47000))
    validset = pickle.load(open(data_path + "validation.p", "rb"))

    train_labeled_loader = torch.utils.data.DataLoader(trainset_labeled,
                                                       batch_size=train_batch_size,
                                                       shuffle=True, **kwargs)

    train_unlabeled_loader = torch.utils.data.DataLoader(trainset_unlabeled,
                                                         batch_size=train_batch_size,
                                                         shuffle=True, **kwargs)

    valid_loader = torch.utils.data.DataLoader(validset, batch_size=valid_batch_size, shuffle=True)

    return train_labeled_loader, train_unlabeled_loader, valid_loader

##################################
# Define Networks
##################################
# Encoder
class Q_net(nn.Module):
    def __init__(self):
        super(Q_net, self).__init__()
        self.lin1 = nn.Linear(X_dim, N)
        self.lin2 = nn.Linear(N, N)
        # Categorical code (y)
        self.lin3cat = nn.Linear(N, n_classes)
        # Gaussian code (z)
        self.lin3gauss = nn.Linear(N, z_dim)

    def forward(self, x):
        x = F.droppout(self.lin1(x), p=0.25, training=self.training)
        x = F.relu(x)
        x = F.droppout(self.lin2(x), p=0.25, training=self.training)
        x = F.relu(x)
        xcat = F.softmax(self.lin3cat(x))
        xgauss = self.lin3gauss(x)

        return xcat, xgauss

# Decoder
class P_net(nn.Module):
    def __init__(self):
        super(P_net, self).__init__()
        self.lin1 = nn.Linear(z_dim + n_classes, N)
        self.lin2 = nn.Linear(N, N)
        self.lin3 = nn.Linear(N, X_dim)

    def forward(self, x):
        x = self.lin1(x)
        x = F.dropout(x, p=0.25, training=self.training)
        x = F.relu(x)
        x = self.lin2(x)
        x = F.dropout(x, p=0.25, training=self.training)
        x = self.lin3(x)
        return F.sigmoid(x)


# Discriminator networks
class D_net_cat(nn.Module):
    def __init__(self):
        super(D_net_cat, self).__init__()
        self.lin1 = nn.Linear(n_classes, N)
        self.lin2 = nn.Linear(N, N)
        self.lin3 = nn.Linear(N, 1)

    def forward(self, x):
        x = F.dropout(self.lin1(x), p=0.2, training=self.training)
        x = F.relu(x)
        F.dropout(self.lin2(x), p=0.2, training=self.training)
        x = F.relu(x)
        return F.sigmoid(self.lin3(x))


class D_net_gauss(nn.Module):
    def __init__(self):
        super(D_net_gauss, self).__init__()
        self.lin1 = nn.Linear(z_dim, N)
        self.lin2 = nn.Linear(N, N)
        self.lin3 = nn.Linear(N, 1)

    def forward(self, x):
        x = F.dropout(self.lin1(x), p=0.2, training=self.training)
        x = F.relu(x)
        x = F.dropout(self.lin2(x), p=0.2, training=self.training)
        x = F.relu(x)

        return F.sigmoid(self.lin3(x))


####################
# Utility functions
####################
def save_model(model, filename):
    print('Best model so far, saving it...')
    torch.save(model.state_dict(), filename)

def sample_categorical(batch_size, n_classes=10):
    '''
     Sample from a categorical distribution
     of size batch_size and # of classes n_classes
     return: torch.autograd.Variable with the sample
    '''
    cat = np.random.randint(0, 10, batch_size)
    cat = np.eye(n_classes)[cat].astype('float32')
    cat = torch.from_numpy(cat)
    return Variable(cat)


def report_loss(epoch, D_loss_cat, D_loss_gauss, G_loss, recon_loss):
    '''
    Print loss
    '''
    print('Epoch-{}; D_loss_cat: {:.4}: D_loss_gauss: {:.4}; G_loss: {:.4}; recon_loss: {:.4}'.format(epoch,
                                                                                                      D_loss_cat.data[0],
                                                                                                      D_loss_gauss.data[0],
                                                                                                      G_loss.data[0],
                                                                                                      recon_loss.data[0]))


def create_latent(Q, loader):
    '''
    Creates the latent representation for the samples in loader
    return:
        z_values: numpy array with the latent representations
        labels: the labels corresponding to the latent representations
    '''
    Q.eval()
    labels = []

    for batch_idx, (X, target) in enumerate(loader):

        X = X * 0.3081 + 0.1307
        # X.resize_(loader.batch_size, X_dim)
        X, target = Variable(X), Variable(target)
        labels.extend(target.data.tolist())
        if cuda:
            X, target = X.cuda(), target.cuda()
        # Reconstruction phase
        z_sample = Q(X)
        if batch_idx > 0:
            z_values = np.concatenate((z_values, np.array(z_sample.data.tolist())))
        else:
            z_values = np.array(z_sample.data.tolist())
    labels = np.array(labels)

    return z_values, labels


def classification_accuracy(Q, data_loader):
    Q.eval()
    labels = []
    test_loss = 0
    correct = 0

    for batch_idx, (X, target) in enumerate(data_loader):
        X = X * 0.3081 + 0.1307
        X.resize_(data_loader.batch_size, X_dim)
        X, target = Variable(X), Variable(target)
        if cuda:
            X, target = X.cuda(), target.cuda()

        labels.extend(target.data.tolist())
        # Reconstruction phase
        output = Q(X)[0]

        test_loss += F.nll_loss(output, target).data[0]

        pred = output.data.max(1)[1]
        correct += pred.eq(target.data).cpu().sum()

    test_loss /= len(data_loader)
    return 100. * correct / len(data_loader.dataset)

def newdataset():
    z_cat = np.arange(0, n_classes)
    z_cat = np.eye(n_classes)[z_cat].astype('float32')
    z_cat = torch.from_numpy(z_cat)
    z_cat = Variable(z_cat)
    if cuda:
        z_cat = z_cat.cuda()
    
    z_real_gauss = Variable(torch.randn(10, z_dim))
    if cuda:
        z_real_gauss = z_real_gauss.cuda()
    X_sample = P(torch.cat((z_cat, z_real_gauss),1))

    for i in range(5000):
        z_real_gauss = Variable(torch.randn(10, z_dim))
        if cuda:
            z_real_gauss = z_real_gauss.cuda()
        x_sample = P(torch.cat((z_cat, z_real_gauss),1))
        X_sample = torch.cat((x_sample, X_sample),0)

    xs = np.array(X_sample.data.tolist())

    pkl.dump(xs, open('./synthetic_samples', 'w'))

####################
# Train procedure
####################
def train(P, Q, D_cat, D_gauss, P_decoder, Q_encoder, Q_semi_supervised, Q_generator, D_cat_solver, D_gauss_solver, train_labeled_loader, train_unlabeled_loader):
    '''
    Train procedure for one epoch.
    '''
    TINY = 1e-15
    # Set the networks in train mode (apply dropout when needed)
    Q.train()
    P.train()
    D_cat.train()
    D_gauss.train()

    if train_unlabeled_loader is None:
        train_unlabeled_loader = train_labeled_loader

    # Loop through the labeled and unlabeled dataset getting one batch of samples from each
    # The batch size has to be a divisor of the size of the dataset or it will return
    # invalid samples
    for (X_l, target_l), (X_u, target_u) in itertools.izip(train_labeled_loader, train_unlabeled_loader):

        for X, target in [(X_u, target_u), (X_l, target_l)]:
            if target[0] == -1:
                labeled = False
            else:
                labeled = True

            # Load batch and normalize samples to be between 0 and 1
            X = X * 0.3081 + 0.1307
            X.resize_(train_batch_size, X_dim)

            X, target = Variable(X), Variable(target)
            if cuda:
                X, target = X.cuda(), target.cuda()

            # Init gradients
            P.zero_grad()
            Q.zero_grad()
            D_cat.zero_grad()
            D_gauss.zero_grad()

            #######################
            # Reconstruction phase
            #######################
            if not labeled:
                z_sample = torch.cat(Q(X), 1)
                X_sample = P(z_sample)

                recon_loss = F.binary_cross_entropy(X_sample + TINY, X.resize(train_batch_size, X_dim) + TINY)
                recon_loss = recon_loss
                recon_loss.backward()
                P_decoder.step()
                Q_encoder.step()

                P.zero_grad()
                Q.zero_grad()
                D_cat.zero_grad()
                D_gauss.zero_grad()
                recon_loss = recon_loss
                #######################
                # Regularization phase
                #######################
                # Discriminator
                Q.eval()
                z_real_cat = sample_categorical(train_batch_size, n_classes=n_classes)
                z_real_gauss = Variable(torch.randn(train_batch_size, z_dim))
                if cuda:
                    z_real_cat = z_real_cat.cuda()
                    z_real_gauss = z_real_gauss.cuda()

                z_fake_cat, z_fake_gauss = Q(X)

                D_real_cat = D_cat(z_real_cat)
                D_real_gauss = D_gauss(z_real_gauss)
                D_fake_cat = D_cat(z_fake_cat)
                D_fake_gauss = D_gauss(z_fake_gauss)

                D_loss_cat = -torch.mean(torch.log(D_real_cat + TINY) + torch.log(1 - D_fake_cat + TINY))
                D_loss_gauss = -torch.mean(torch.log(D_real_gauss + TINY) + torch.log(1 - D_fake_gauss + TINY))

                D_loss = D_loss_cat + D_loss_gauss
                D_loss = D_loss

                D_loss.backward()
                D_cat_solver.step()
                D_gauss_solver.step()

                P.zero_grad()
                Q.zero_grad()
                D_cat.zero_grad()
                D_gauss.zero_grad()

                # Generator
                Q.train()
                z_fake_cat, z_fake_gauss = Q(X)

                D_fake_cat = D_cat(z_fake_cat)
                D_fake_gauss = D_gauss(z_fake_gauss)

                G_loss = - torch.mean(torch.log(D_fake_cat + TINY)) - torch.mean(torch.log(D_fake_gauss + TINY))
                G_loss = G_loss
                G_loss.backward()
                Q_generator.step()

                P.zero_grad()
                Q.zero_grad()
                D_cat.zero_grad()
                D_gauss.zero_grad()

            #######################
            # Semi-supervised phase
            #######################
            if labeled:
                pred, _ = Q(X)
                class_loss = F.cross_entropy(pred, target)
                class_loss.backward()
                Q_semi_supervised.step()

                P.zero_grad()
                Q.zero_grad()
                D_cat.zero_grad()
                D_gauss.zero_grad()

    return D_loss_cat, D_loss_gauss, G_loss, recon_loss, class_loss


def generate_model(savefolder='./'):
    train_labeled_loader, train_unlabeled_loader, valid_loader = load_data()

    # Create and initialize Networks
    train_acc = []
    val_acc = []

    torch.manual_seed(10)

    if cuda:
        Q = Q_net().cuda()
        P = P_net().cuda()
        D_cat = D_net_cat().cuda()
        D_gauss = D_net_gauss().cuda()
    else:
        Q = Q_net()
        P = P_net()
        D_gauss = D_net_gauss()
        D_cat = D_net_cat()

    # Set learning rates
    gen_lr = 0.0006
    semi_lr = 0.001
    reg_lr = 0.0008

    # Set optimizators
    P_decoder = optim.Adam(P.parameters(), lr=gen_lr)
    Q_encoder = optim.Adam(Q.parameters(), lr=gen_lr)

    Q_semi_supervised = optim.Adam(Q.parameters(), lr=semi_lr)

    Q_generator = optim.Adam(Q.parameters(), lr=reg_lr)
    D_gauss_solver = optim.Adam(D_gauss.parameters(), lr=reg_lr)
    D_cat_solver = optim.Adam(D_cat.parameters(), lr=reg_lr)

    for epoch in range(epochs):
        if epoch == 75:
            P_decoder = optim.Adam(P.parameters(), lr=gen_lr / 10.)
            Q_encoder = optim.Adam(Q.parameters(), lr=gen_lr / 10.)

            Q_semi_supervised = optim.Adam(Q.parameters(), lr=semi_lr / 10.)

            Q_generator = optim.Adam(Q.parameters(), lr=reg_lr / 10.)
            D_gauss_solver = optim.Adam(D_gauss.parameters(), lr=reg_lr / 10.)
            D_cat_solver = optim.Adam(D_cat.parameters(), lr=reg_lr / 10.)

        D_loss_cat, D_loss_gauss, G_loss, recon_loss, class_loss = train(P, Q, D_cat,
                                                                         D_gauss, P_decoder,
                                                                         Q_encoder, Q_semi_supervised,
                                                                         Q_generator,
                                                                         D_cat_solver, D_gauss_solver,
                                                                         train_labeled_loader,
                                                                         train_unlabeled_loader)
        if epoch % 10 == 0:
            train_acc.append(classification_accuracy(Q, train_labeled_loader))
            val_acc.append(classification_accuracy(Q, valid_loader))
            report_loss(epoch, D_loss_cat, D_loss_gauss, G_loss, recon_loss)
            print('Classification Loss: {:.3}'.format(class_loss.data[0]))
            print('Validation accuracy: {} %'.format(val_acc[-1]))

    torch.save(Q.state_dict(), savefolder + 'Q_networkL')
    torch.save(P.state_dict(), savefolder + 'P_network')
    torch.save(D_cat.state_dict(), savefolder + 'D_cat_network')
    torch.save(D_gauss.state_dict(), savefolder + 'D_gauss_network')


if __name__ == '__main__':
    generate_model()







import argparse
import time
import torch
import pickle
import numpy as np
import itertools
from torch.autograd import Variable
import torch.nn as nn
import torch.nn.functional as F
import torch.autograd as autograd
import torch.optim as optim
from torch.nn.modules.upsampling import UpsamplingNearest2d

# Training settings
parser = argparse.ArgumentParser(description='PyTorch semi-supervised MNIST')

parser.add_argument('--batch-size', type=int, default=100, metavar='N',
                    help='input batch size for training (default: 100)')
parser.add_argument('--epochs', type=int, default=5000, metavar='N',
                    help='number of epochs to train (default: 10)')
parser.add_argument('--no-cuda', action='store_true', default=False,
                    help='enables CUDA training')


args = parser.parse_args()
args.cuda = not args.no_cuda and torch.cuda.is_available()

seed = 10
if args.cuda:
    torch.cuda.manual_seed(seed)
cuda = args.cuda

kwargs = {'num_workers': 1, 'pin_memory': True} if cuda else {}
n_classes = 10
z_dim = 10
X_dim = 784
y_dim = 10
convolutional = False
train_batch_size = args.batch_size
valid_batch_size = args.batch_size
N = 1000
epochs = args.epochs

##################################
# Load data and create Data loaders
##################################
def load_data(data_path='../data/'):
    print('loading data!')
    trainset_labeled = pickle.load(open(data_path + "train_labeled.p", "rb"))
    trainset_unlabeled = pickle.load(open(data_path + "train_unlabeled.p", "rb"))
    # Set -1 as labels for unlabeled data
    trainset_unlabeled.train_labels = torch.from_numpy(np.array([-1] * 47000))
    validset = pickle.load(open(data_path + "validation.p", "rb"))

    train_labeled_loader = torch.utils.data.DataLoader(trainset_labeled,
                                                       batch_size=train_batch_size,
                                                       shuffle=True, **kwargs)

    train_unlabeled_loader = torch.utils.data.DataLoader(trainset_unlabeled,
                                                         batch_size=train_batch_size,
                                                         shuffle=True, **kwargs)

    valid_loader = torch.utils.data.DataLoader(validset, batch_size=valid_batch_size, shuffle=True)

    return train_labeled_loader, train_unlabeled_loader, valid_loader

##################################
# Define Networks
##################################
# Encoder
class Q_net(nn.Module):
    def __init__(self):
        super(Q_net, self).__init__()
        self.lin1 = nn.Linear(X_dim, N)
        self.lin2 = nn.Linear(N, N)
        # Categorical code (y)
        self.lin3cat = nn.Linear(N, n_classes)
        # Gaussian code (z)
        self.lin3gauss = nn.Linear(N, z_dim)

    def forward(self, x):
        x = F.droppout(self.lin1(x), p=0.25, training=self.training)
        x = F.relu(x)
        x = F.droppout(self.lin2(x), p=0.25, training=self.training)
        x = F.relu(x)
        xcat = F.softmax(self.lin3cat(x))
        xgauss = self.lin3gauss(x)

        return xcat, xgauss

# Decoder
class P_net(nn.Module):
    def __init__(self):
        super(P_net, self).__init__()
        self.lin1 = nn.Linear(z_dim + n_classes, N)
        self.lin2 = nn.Linear(N, N)
        self.lin3 = nn.Linear(N, X_dim)

    def forward(self, x):
        x = self.lin1(x)
        x = F.dropout(x, p=0.25, training=self.training)
        x = F.relu(x)
        x = self.lin2(x)
        x = F.dropout(x, p=0.25, training=self.training)
        x = self.lin3(x)
        return F.sigmoid(x)


# Discriminator networks
class D_net_cat(nn.Module):
    def __init__(self):
        super(D_net_cat, self).__init__()
        self.lin1 = nn.Linear(n_classes, N)
        self.lin2 = nn.Linear(N, N)
        self.lin3 = nn.Linear(N, 1)

    def forward(self, x):
        x = F.dropout(self.lin1(x), p=0.2, training=self.training)
        x = F.relu(x)
        F.dropout(self.lin2(x), p=0.2, training=self.training)
        x = F.relu(x)
        return F.sigmoid(self.lin3(x))


class D_net_gauss(nn.Module):
    def __init__(self):
        super(D_net_gauss, self).__init__()
        self.lin1 = nn.Linear(z_dim, N)
        self.lin2 = nn.Linear(N, N)
        self.lin3 = nn.Linear(N, 1)

    def forward(self, x):
        x = F.dropout(self.lin1(x), p=0.2, training=self.training)
        x = F.relu(x)
        x = F.dropout(self.lin2(x), p=0.2, training=self.training)
        x = F.relu(x)

        return F.sigmoid(self.lin3(x))


####################
# Utility functions
####################
def save_model(model, filename):
    print('Best model so far, saving it...')
    torch.save(model.state_dict(), filename)

def sample_categorical(batch_size, n_classes=10):
    '''
     Sample from a categorical distribution
     of size batch_size and # of classes n_classes
     return: torch.autograd.Variable with the sample
    '''
    cat = np.random.randint(0, 10, batch_size)
    cat = np.eye(n_classes)[cat].astype('float32')
    cat = torch.from_numpy(cat)
    return Variable(cat)


def report_loss(epoch, D_loss_cat, D_loss_gauss, G_loss, recon_loss):
    '''
    Print loss
    '''
    print('Epoch-{}; D_loss_cat: {:.4}: D_loss_gauss: {:.4}; G_loss: {:.4}; recon_loss: {:.4}'.format(epoch,
                                                                                                      D_loss_cat.data[0],
                                                                                                      D_loss_gauss.data[0],
                                                                                                      G_loss.data[0],
                                                                                                      recon_loss.data[0]))


def create_latent(Q, loader):
    '''
    Creates the latent representation for the samples in loader
    return:
        z_values: numpy array with the latent representations
        labels: the labels corresponding to the latent representations
    '''
    Q.eval()
    labels = []

    for batch_idx, (X, target) in enumerate(loader):

        X = X * 0.3081 + 0.1307
        # X.resize_(loader.batch_size, X_dim)
        X, target = Variable(X), Variable(target)
        labels.extend(target.data.tolist())
        if cuda:
            X, target = X.cuda(), target.cuda()
        # Reconstruction phase
        z_sample = Q(X)
        if batch_idx > 0:
            z_values = np.concatenate((z_values, np.array(z_sample.data.tolist())))
        else:
            z_values = np.array(z_sample.data.tolist())
    labels = np.array(labels)

    return z_values, labels


def classification_accuracy(Q, data_loader):
    Q.eval()
    labels = []
    test_loss = 0
    correct = 0

    for batch_idx, (X, target) in enumerate(data_loader):
        X = X * 0.3081 + 0.1307
        X.resize_(data_loader.batch_size, X_dim)
        X, target = Variable(X), Variable(target)
        if cuda:
            X, target = X.cuda(), target.cuda()

        labels.extend(target.data.tolist())
        # Reconstruction phase
        output = Q(X)[0]

        test_loss += F.nll_loss(output, target).data[0]

        pred = output.data.max(1)[1]
        correct += pred.eq(target.data).cpu().sum()

    test_loss /= len(data_loader)
    return 100. * correct / len(data_loader.dataset)

def newdataset():
    z_cat = np.arange(0, n_classes)
    z_cat = np.eye(n_classes)[z_cat].astype('float32')
    z_cat = torch.from_numpy(z_cat)
    z_cat = Variable(z_cat)
    if cuda:
        z_cat = z_cat.cuda()
    
    z_real_gauss = Variable(torch.randn(10, z_dim))
    if cuda:
        z_real_gauss = z_real_gauss.cuda()
    X_sample = P(torch.cat((z_cat, z_real_gauss),1))

    for i in range(5000):
        z_real_gauss = Variable(torch.randn(10, z_dim))
        if cuda:
            z_real_gauss = z_real_gauss.cuda()
        x_sample = P(torch.cat((z_cat, z_real_gauss),1))
        X_sample = torch.cat((x_sample, X_sample),0)

    xs = np.array(X_sample.data.tolist())

    pkl.dump(xs, open('./synthetic_samples', 'w'))

####################
# Train procedure
####################
def train(P, Q, D_cat, D_gauss, P_decoder, Q_encoder, Q_semi_supervised, Q_generator, D_cat_solver, D_gauss_solver, train_labeled_loader, train_unlabeled_loader):
    '''
    Train procedure for one epoch.
    '''
    TINY = 1e-15
    # Set the networks in train mode (apply dropout when needed)
    Q.train()
    P.train()
    D_cat.train()
    D_gauss.train()

    if train_unlabeled_loader is None:
        train_unlabeled_loader = train_labeled_loader

    # Loop through the labeled and unlabeled dataset getting one batch of samples from each
    # The batch size has to be a divisor of the size of the dataset or it will return
    # invalid samples
    for (X_l, target_l), (X_u, target_u) in itertools.izip(train_labeled_loader, train_unlabeled_loader):

        for X, target in [(X_u, target_u), (X_l, target_l)]:
            if target[0] == -1:
                labeled = False
            else:
                labeled = True

            # Load batch and normalize samples to be between 0 and 1
            X = X * 0.3081 + 0.1307
            X.resize_(train_batch_size, X_dim)

            X, target = Variable(X), Variable(target)
            if cuda:
                X, target = X.cuda(), target.cuda()

            # Init gradients
            P.zero_grad()
            Q.zero_grad()
            D_cat.zero_grad()
            D_gauss.zero_grad()

            #######################
            # Reconstruction phase
            #######################
            if not labeled:
                z_sample = torch.cat(Q(X), 1)
                X_sample = P(z_sample)

                recon_loss = F.binary_cross_entropy(X_sample + TINY, X.resize(train_batch_size, X_dim) + TINY)
                recon_loss = recon_loss
                recon_loss.backward()
                P_decoder.step()
                Q_encoder.step()

                P.zero_grad()
                Q.zero_grad()
                D_cat.zero_grad()
                D_gauss.zero_grad()
                recon_loss = recon_loss
                #######################
                # Regularization phase
                #######################
                # Discriminator
                Q.eval()
                z_real_cat = sample_categorical(train_batch_size, n_classes=n_classes)
                z_real_gauss = Variable(torch.randn(train_batch_size, z_dim))
                if cuda:
                    z_real_cat = z_real_cat.cuda()
                    z_real_gauss = z_real_gauss.cuda()

                z_fake_cat, z_fake_gauss = Q(X)

                D_real_cat = D_cat(z_real_cat)
                D_real_gauss = D_gauss(z_real_gauss)
                D_fake_cat = D_cat(z_fake_cat)
                D_fake_gauss = D_gauss(z_fake_gauss)

                D_loss_cat = -torch.mean(torch.log(D_real_cat + TINY) + torch.log(1 - D_fake_cat + TINY))
                D_loss_gauss = -torch.mean(torch.log(D_real_gauss + TINY) + torch.log(1 - D_fake_gauss + TINY))

                D_loss = D_loss_cat + D_loss_gauss
                D_loss = D_loss

                D_loss.backward()
                D_cat_solver.step()
                D_gauss_solver.step()

                P.zero_grad()
                Q.zero_grad()
                D_cat.zero_grad()
                D_gauss.zero_grad()

                # Generator
                Q.train()
                z_fake_cat, z_fake_gauss = Q(X)

                D_fake_cat = D_cat(z_fake_cat)
                D_fake_gauss = D_gauss(z_fake_gauss)

                G_loss = - torch.mean(torch.log(D_fake_cat + TINY)) - torch.mean(torch.log(D_fake_gauss + TINY))
                G_loss = G_loss
                G_loss.backward()
                Q_generator.step()

                P.zero_grad()
                Q.zero_grad()
                D_cat.zero_grad()
                D_gauss.zero_grad()

            #######################
            # Semi-supervised phase
            #######################
            if labeled:
                pred, _ = Q(X)
                class_loss = F.cross_entropy(pred, target)
                class_loss.backward()
                Q_semi_supervised.step()

                P.zero_grad()
                Q.zero_grad()
                D_cat.zero_grad()
                D_gauss.zero_grad()

    return D_loss_cat, D_loss_gauss, G_loss, recon_loss, class_loss


def generate_model(savefolder='./'):
    train_labeled_loader, train_unlabeled_loader, valid_loader = load_data()

    # Create and initialize Networks
    train_acc = []
    val_acc = []

    torch.manual_seed(10)

    if cuda:
        Q = Q_net().cuda()
        P = P_net().cuda()
        D_cat = D_net_cat().cuda()
        D_gauss = D_net_gauss().cuda()
    else:
        Q = Q_net()
        P = P_net()
        D_gauss = D_net_gauss()
        D_cat = D_net_cat()

    # Set learning rates
    gen_lr = 0.0006
    semi_lr = 0.001
    reg_lr = 0.0008

    # Set optimizators
    P_decoder = optim.Adam(P.parameters(), lr=gen_lr)
    Q_encoder = optim.Adam(Q.parameters(), lr=gen_lr)

    Q_semi_supervised = optim.Adam(Q.parameters(), lr=semi_lr)

    Q_generator = optim.Adam(Q.parameters(), lr=reg_lr)
    D_gauss_solver = optim.Adam(D_gauss.parameters(), lr=reg_lr)
    D_cat_solver = optim.Adam(D_cat.parameters(), lr=reg_lr)

    for epoch in range(epochs):
        if epoch == 75:
            P_decoder = optim.Adam(P.parameters(), lr=gen_lr / 10.)
            Q_encoder = optim.Adam(Q.parameters(), lr=gen_lr / 10.)

            Q_semi_supervised = optim.Adam(Q.parameters(), lr=semi_lr / 10.)

            Q_generator = optim.Adam(Q.parameters(), lr=reg_lr / 10.)
            D_gauss_solver = optim.Adam(D_gauss.parameters(), lr=reg_lr / 10.)
            D_cat_solver = optim.Adam(D_cat.parameters(), lr=reg_lr / 10.)

        D_loss_cat, D_loss_gauss, G_loss, recon_loss, class_loss = train(P, Q, D_cat,
                                                                         D_gauss, P_decoder,
                                                                         Q_encoder, Q_semi_supervised,
                                                                         Q_generator,
                                                                         D_cat_solver, D_gauss_solver,
                                                                         train_labeled_loader,
                                                                         train_unlabeled_loader)
        if epoch % 10 == 0:
            train_acc.append(classification_accuracy(Q, train_labeled_loader))
            val_acc.append(classification_accuracy(Q, valid_loader))
            report_loss(epoch, D_loss_cat, D_loss_gauss, G_loss, recon_loss)
            print('Classification Loss: {:.3}'.format(class_loss.data[0]))
            print('Validation accuracy: {} %'.format(val_acc[-1]))

    torch.save(Q.state_dict(), savefolder + 'Q_networkL')
    torch.save(P.state_dict(), savefolder + 'P_network')
    torch.save(D_cat.state_dict(), savefolder + 'D_cat_network')
    torch.save(D_gauss.state_dict(), savefolder + 'D_gauss_network')


if __name__ == '__main__':
    generate_model()







import argparse
import time
import torch
import pickle
import numpy as np
import itertools
from torch.autograd import Variable
import torch.nn as nn
import torch.nn.functional as F
import torch.autograd as autograd
import torch.optim as optim
from torch.nn.modules.upsampling import UpsamplingNearest2d

# Training settings
parser = argparse.ArgumentParser(description='PyTorch semi-supervised MNIST')

parser.add_argument('--batch-size', type=int, default=100, metavar='N',
                    help='input batch size for training (default: 100)')
parser.add_argument('--epochs', type=int, default=5000, metavar='N',
                    help='number of epochs to train (default: 10)')
parser.add_argument('--no-cuda', action='store_true', default=False,
                    help='enables CUDA training')


args = parser.parse_args()
args.cuda = not args.no_cuda and torch.cuda.is_available()

seed = 10
if args.cuda:
    torch.cuda.manual_seed(seed)
cuda = args.cuda

kwargs = {'num_workers': 1, 'pin_memory': True} if cuda else {}
n_classes = 10
z_dim = 10
X_dim = 784
y_dim = 10
convolutional = False
train_batch_size = args.batch_size
valid_batch_size = args.batch_size
N = 1000
epochs = args.epochs

##################################
# Load data and create Data loaders
##################################
def load_data(data_path='../data/'):
    print('loading data!')
    trainset_labeled = pickle.load(open(data_path + "train_labeled.p", "rb"))
    trainset_unlabeled = pickle.load(open(data_path + "train_unlabeled.p", "rb"))
    # Set -1 as labels for unlabeled data
    trainset_unlabeled.train_labels = torch.from_numpy(np.array([-1] * 47000))
    validset = pickle.load(open(data_path + "validation.p", "rb"))

    train_labeled_loader = torch.utils.data.DataLoader(trainset_labeled,
                                                       batch_size=train_batch_size,
                                                       shuffle=True, **kwargs)

    train_unlabeled_loader = torch.utils.data.DataLoader(trainset_unlabeled,
                                                         batch_size=train_batch_size,
                                                         shuffle=True, **kwargs)

    valid_loader = torch.utils.data.DataLoader(validset, batch_size=valid_batch_size, shuffle=True)

    return train_labeled_loader, train_unlabeled_loader, valid_loader

##################################
# Define Networks
##################################
# Encoder
class Q_net(nn.Module):
    def __init__(self):
        super(Q_net, self).__init__()
        self.lin1 = nn.Linear(X_dim, N)
        self.lin2 = nn.Linear(N, N)
        # Categorical code (y)
        self.lin3cat = nn.Linear(N, n_classes)
        # Gaussian code (z)
        self.lin3gauss = nn.Linear(N, z_dim)

    def forward(self, x):
        x = F.droppout(self.lin1(x), p=0.25, training=self.training)
        x = F.relu(x)
        x = F.droppout(self.lin2(x), p=0.25, training=self.training)
        x = F.relu(x)
        xcat = F.softmax(self.lin3cat(x))
        xgauss = self.lin3gauss(x)

        return xcat, xgauss

# Decoder
class P_net(nn.Module):
    def __init__(self):
        super(P_net, self).__init__()
        self.lin1 = nn.Linear(z_dim + n_classes, N)
        self.lin2 = nn.Linear(N, N)
        self.lin3 = nn.Linear(N, X_dim)

    def forward(self, x):
        x = self.lin1(x)
        x = F.dropout(x, p=0.25, training=self.training)
        x = F.relu(x)
        x = self.lin2(x)
        x = F.dropout(x, p=0.25, training=self.training)
        x = self.lin3(x)
        return F.sigmoid(x)


# Discriminator networks
class D_net_cat(nn.Module):
    def __init__(self):
        super(D_net_cat, self).__init__()
        self.lin1 = nn.Linear(n_classes, N)
        self.lin2 = nn.Linear(N, N)
        self.lin3 = nn.Linear(N, 1)

    def forward(self, x):
        x = F.dropout(self.lin1(x), p=0.2, training=self.training)
        x = F.relu(x)
        F.dropout(self.lin2(x), p=0.2, training=self.training)
        x = F.relu(x)
        return F.sigmoid(self.lin3(x))


class D_net_gauss(nn.Module):
    def __init__(self):
        super(D_net_gauss, self).__init__()
        self.lin1 = nn.Linear(z_dim, N)
        self.lin2 = nn.Linear(N, N)
        self.lin3 = nn.Linear(N, 1)

    def forward(self, x):
        x = F.dropout(self.lin1(x), p=0.2, training=self.training)
        x = F.relu(x)
        x = F.dropout(self.lin2(x), p=0.2, training=self.training)
        x = F.relu(x)

        return F.sigmoid(self.lin3(x))


####################
# Utility functions
####################
def save_model(model, filename):
    print('Best model so far, saving it...')
    torch.save(model.state_dict(), filename)

def sample_categorical(batch_size, n_classes=10):
    '''
     Sample from a categorical distribution
     of size batch_size and # of classes n_classes
     return: torch.autograd.Variable with the sample
    '''
    cat = np.random.randint(0, 10, batch_size)
    cat = np.eye(n_classes)[cat].astype('float32')
    cat = torch.from_numpy(cat)
    return Variable(cat)


def report_loss(epoch, D_loss_cat, D_loss_gauss, G_loss, recon_loss):
    '''
    Print loss
    '''
    print('Epoch-{}; D_loss_cat: {:.4}: D_loss_gauss: {:.4}; G_loss: {:.4}; recon_loss: {:.4}'.format(epoch,
                                                                                                      D_loss_cat.data[0],
                                                                                                      D_loss_gauss.data[0],
                                                                                                      G_loss.data[0],
                                                                                                      recon_loss.data[0]))


def create_latent(Q, loader):
    '''
    Creates the latent representation for the samples in loader
    return:
        z_values: numpy array with the latent representations
        labels: the labels corresponding to the latent representations
    '''
    Q.eval()
    labels = []

    for batch_idx, (X, target) in enumerate(loader):

        X = X * 0.3081 + 0.1307
        # X.resize_(loader.batch_size, X_dim)
        X, target = Variable(X), Variable(target)
        labels.extend(target.data.tolist())
        if cuda:
            X, target = X.cuda(), target.cuda()
        # Reconstruction phase
        z_sample = Q(X)
        if batch_idx > 0:
            z_values = np.concatenate((z_values, np.array(z_sample.data.tolist())))
        else:
            z_values = np.array(z_sample.data.tolist())
    labels = np.array(labels)

    return z_values, labels


def classification_accuracy(Q, data_loader):
    Q.eval()
    labels = []
    test_loss = 0
    correct = 0

    for batch_idx, (X, target) in enumerate(data_loader):
        X = X * 0.3081 + 0.1307
        X.resize_(data_loader.batch_size, X_dim)
        X, target = Variable(X), Variable(target)
        if cuda:
            X, target = X.cuda(), target.cuda()

        labels.extend(target.data.tolist())
        # Reconstruction phase
        output = Q(X)[0]

        test_loss += F.nll_loss(output, target).data[0]

        pred = output.data.max(1)[1]
        correct += pred.eq(target.data).cpu().sum()

    test_loss /= len(data_loader)
    return 100. * correct / len(data_loader.dataset)

def newdataset():
    z_cat = np.arange(0, n_classes)
    z_cat = np.eye(n_classes)[z_cat].astype('float32')
    z_cat = torch.from_numpy(z_cat)
    z_cat = Variable(z_cat)
    if cuda:
        z_cat = z_cat.cuda()
    
    z_real_gauss = Variable(torch.randn(10, z_dim))
    if cuda:
        z_real_gauss = z_real_gauss.cuda()
    X_sample = P(torch.cat((z_cat, z_real_gauss),1))

    for i in range(5000):
        z_real_gauss = Variable(torch.randn(10, z_dim))
        if cuda:
            z_real_gauss = z_real_gauss.cuda()
        x_sample = P(torch.cat((z_cat, z_real_gauss),1))
        X_sample = torch.cat((x_sample, X_sample),0)

    xs = np.array(X_sample.data.tolist())

    pkl.dump(xs, open('./synthetic_samples', 'w'))

####################
# Train procedure
####################
def train(P, Q, D_cat, D_gauss, P_decoder, Q_encoder, Q_semi_supervised, Q_generator, D_cat_solver, D_gauss_solver, train_labeled_loader, train_unlabeled_loader):
    '''
    Train procedure for one epoch.
    '''
    TINY = 1e-15
    # Set the networks in train mode (apply dropout when needed)
    Q.train()
    P.train()
    D_cat.train()
    D_gauss.train()

    if train_unlabeled_loader is None:
        train_unlabeled_loader = train_labeled_loader

    # Loop through the labeled and unlabeled dataset getting one batch of samples from each
    # The batch size has to be a divisor of the size of the dataset or it will return
    # invalid samples
    for (X_l, target_l), (X_u, target_u) in itertools.izip(train_labeled_loader, train_unlabeled_loader):

        for X, target in [(X_u, target_u), (X_l, target_l)]:
            if target[0] == -1:
                labeled = False
            else:
                labeled = True

            # Load batch and normalize samples to be between 0 and 1
            X = X * 0.3081 + 0.1307
            X.resize_(train_batch_size, X_dim)

            X, target = Variable(X), Variable(target)
            if cuda:
                X, target = X.cuda(), target.cuda()

            # Init gradients
            P.zero_grad()
            Q.zero_grad()
            D_cat.zero_grad()
            D_gauss.zero_grad()

            #######################
            # Reconstruction phase
            #######################
            if not labeled:
                z_sample = torch.cat(Q(X), 1)
                X_sample = P(z_sample)

                recon_loss = F.binary_cross_entropy(X_sample + TINY, X.resize(train_batch_size, X_dim) + TINY)
                recon_loss = recon_loss
                recon_loss.backward()
                P_decoder.step()
                Q_encoder.step()

                P.zero_grad()
                Q.zero_grad()
                D_cat.zero_grad()
                D_gauss.zero_grad()
                recon_loss = recon_loss
                #######################
                # Regularization phase
                #######################
                # Discriminator
                Q.eval()
                z_real_cat = sample_categorical(train_batch_size, n_classes=n_classes)
                z_real_gauss = Variable(torch.randn(train_batch_size, z_dim))
                if cuda:
                    z_real_cat = z_real_cat.cuda()
                    z_real_gauss = z_real_gauss.cuda()

                z_fake_cat, z_fake_gauss = Q(X)

                D_real_cat = D_cat(z_real_cat)
                D_real_gauss = D_gauss(z_real_gauss)
                D_fake_cat = D_cat(z_fake_cat)
                D_fake_gauss = D_gauss(z_fake_gauss)

                D_loss_cat = -torch.mean(torch.log(D_real_cat + TINY) + torch.log(1 - D_fake_cat + TINY))
                D_loss_gauss = -torch.mean(torch.log(D_real_gauss + TINY) + torch.log(1 - D_fake_gauss + TINY))

                D_loss = D_loss_cat + D_loss_gauss
                D_loss = D_loss

                D_loss.backward()
                D_cat_solver.step()
                D_gauss_solver.step()

                P.zero_grad()
                Q.zero_grad()
                D_cat.zero_grad()
                D_gauss.zero_grad()

                # Generator
                Q.train()
                z_fake_cat, z_fake_gauss = Q(X)

                D_fake_cat = D_cat(z_fake_cat)
                D_fake_gauss = D_gauss(z_fake_gauss)

                G_loss = - torch.mean(torch.log(D_fake_cat + TINY)) - torch.mean(torch.log(D_fake_gauss + TINY))
                G_loss = G_loss
                G_loss.backward()
                Q_generator.step()

                P.zero_grad()
                Q.zero_grad()
                D_cat.zero_grad()
                D_gauss.zero_grad()

            #######################
            # Semi-supervised phase
            #######################
            if labeled:
                pred, _ = Q(X)
                class_loss = F.cross_entropy(pred, target)
                class_loss.backward()
                Q_semi_supervised.step()

                P.zero_grad()
                Q.zero_grad()
                D_cat.zero_grad()
                D_gauss.zero_grad()

    return D_loss_cat, D_loss_gauss, G_loss, recon_loss, class_loss


def generate_model(savefolder='./'):
    train_labeled_loader, train_unlabeled_loader, valid_loader = load_data()

    # Create and initialize Networks
    train_acc = []
    val_acc = []

    torch.manual_seed(10)

    if cuda:
        Q = Q_net().cuda()
        P = P_net().cuda()
        D_cat = D_net_cat().cuda()
        D_gauss = D_net_gauss().cuda()
    else:
        Q = Q_net()
        P = P_net()
        D_gauss = D_net_gauss()
        D_cat = D_net_cat()

    # Set learning rates
    gen_lr = 0.0006
    semi_lr = 0.001
    reg_lr = 0.0008

    # Set optimizators
    P_decoder = optim.Adam(P.parameters(), lr=gen_lr)
    Q_encoder = optim.Adam(Q.parameters(), lr=gen_lr)

    Q_semi_supervised = optim.Adam(Q.parameters(), lr=semi_lr)

    Q_generator = optim.Adam(Q.parameters(), lr=reg_lr)
    D_gauss_solver = optim.Adam(D_gauss.parameters(), lr=reg_lr)
    D_cat_solver = optim.Adam(D_cat.parameters(), lr=reg_lr)

    for epoch in range(epochs):
        if epoch == 75:
            P_decoder = optim.Adam(P.parameters(), lr=gen_lr / 10.)
            Q_encoder = optim.Adam(Q.parameters(), lr=gen_lr / 10.)

            Q_semi_supervised = optim.Adam(Q.parameters(), lr=semi_lr / 10.)

            Q_generator = optim.Adam(Q.parameters(), lr=reg_lr / 10.)
            D_gauss_solver = optim.Adam(D_gauss.parameters(), lr=reg_lr / 10.)
            D_cat_solver = optim.Adam(D_cat.parameters(), lr=reg_lr / 10.)

        D_loss_cat, D_loss_gauss, G_loss, recon_loss, class_loss = train(P, Q, D_cat,
                                                                         D_gauss, P_decoder,
                                                                         Q_encoder, Q_semi_supervised,
                                                                         Q_generator,
                                                                         D_cat_solver, D_gauss_solver,
                                                                         train_labeled_loader,
                                                                         train_unlabeled_loader)
        if epoch % 10 == 0:
            train_acc.append(classification_accuracy(Q, train_labeled_loader))
            val_acc.append(classification_accuracy(Q, valid_loader))
            report_loss(epoch, D_loss_cat, D_loss_gauss, G_loss, recon_loss)
            print('Classification Loss: {:.3}'.format(class_loss.data[0]))
            print('Validation accuracy: {} %'.format(val_acc[-1]))

    torch.save(Q.state_dict(), savefolder + 'Q_networkL')
    torch.save(P.state_dict(), savefolder + 'P_network')
    torch.save(D_cat.state_dict(), savefolder + 'D_cat_network')
    torch.save(D_gauss.state_dict(), savefolder + 'D_gauss_network')


if __name__ == '__main__':
    generate_model()







import argparse
import time
import torch
import pickle
import numpy as np
import itertools
from torch.autograd import Variable
import torch.nn as nn
import torch.nn.functional as F
import torch.autograd as autograd
import torch.optim as optim
from torch.nn.modules.upsampling import UpsamplingNearest2d

# Training settings
parser = argparse.ArgumentParser(description='PyTorch semi-supervised MNIST')

parser.add_argument('--batch-size', type=int, default=100, metavar='N',
                    help='input batch size for training (default: 100)')
parser.add_argument('--epochs', type=int, default=5000, metavar='N',
                    help='number of epochs to train (default: 10)')
parser.add_argument('--no-cuda', action='store_true', default=False,
                    help='enables CUDA training')


args = parser.parse_args()
args.cuda = not args.no_cuda and torch.cuda.is_available()

seed = 10
if args.cuda:
    torch.cuda.manual_seed(seed)
cuda = args.cuda

kwargs = {'num_workers': 1, 'pin_memory': True} if cuda else {}
n_classes = 10
z_dim = 10
X_dim = 784
y_dim = 10
convolutional = False
train_batch_size = args.batch_size
valid_batch_size = args.batch_size
N = 1000
epochs = args.epochs

##################################
# Load data and create Data loaders
##################################
def load_data(data_path='../data/'):
    print('loading data!')
    trainset_labeled = pickle.load(open(data_path + "train_labeled.p", "rb"))
    trainset_unlabeled = pickle.load(open(data_path + "train_unlabeled.p", "rb"))
    # Set -1 as labels for unlabeled data
    trainset_unlabeled.train_labels = torch.from_numpy(np.array([-1] * 47000))
    validset = pickle.load(open(data_path + "validation.p", "rb"))

    train_labeled_loader = torch.utils.data.DataLoader(trainset_labeled,
                                                       batch_size=train_batch_size,
                                                       shuffle=True, **kwargs)

    train_unlabeled_loader = torch.utils.data.DataLoader(trainset_unlabeled,
                                                         batch_size=train_batch_size,
                                                         shuffle=True, **kwargs)

    valid_loader = torch.utils.data.DataLoader(validset, batch_size=valid_batch_size, shuffle=True)

    return train_labeled_loader, train_unlabeled_loader, valid_loader

##################################
# Define Networks
##################################
# Encoder
class Q_net(nn.Module):
    def __init__(self):
        super(Q_net, self).__init__()
        self.lin1 = nn.Linear(X_dim, N)
        self.lin2 = nn.Linear(N, N)
        # Categorical code (y)
        self.lin3cat = nn.Linear(N, n_classes)
        # Gaussian code (z)
        self.lin3gauss = nn.Linear(N, z_dim)

    def forward(self, x):
        x = F.droppout(self.lin1(x), p=0.25, training=self.training)
        x = F.relu(x)
        x = F.droppout(self.lin2(x), p=0.25, training=self.training)
        x = F.relu(x)
        xcat = F.softmax(self.lin3cat(x))
        xgauss = self.lin3gauss(x)

        return xcat, xgauss

# Decoder
class P_net(nn.Module):
    def __init__(self):
        super(P_net, self).__init__()
        self.lin1 = nn.Linear(z_dim + n_classes, N)
        self.lin2 = nn.Linear(N, N)
        self.lin3 = nn.Linear(N, X_dim)

    def forward(self, x):
        x = self.lin1(x)
        x = F.dropout(x, p=0.25, training=self.training)
        x = F.relu(x)
        x = self.lin2(x)
        x = F.dropout(x, p=0.25, training=self.training)
        x = self.lin3(x)
        return F.sigmoid(x)


# Discriminator networks
class D_net_cat(nn.Module):
    def __init__(self):
        super(D_net_cat, self).__init__()
        self.lin1 = nn.Linear(n_classes, N)
        self.lin2 = nn.Linear(N, N)
        self.lin3 = nn.Linear(N, 1)

    def forward(self, x):
        x = F.dropout(self.lin1(x), p=0.2, training=self.training)
        x = F.relu(x)
        F.dropout(self.lin2(x), p=0.2, training=self.training)
        x = F.relu(x)
        return F.sigmoid(self.lin3(x))


class D_net_gauss(nn.Module):
    def __init__(self):
        super(D_net_gauss, self).__init__()
        self.lin1 = nn.Linear(z_dim, N)
        self.lin2 = nn.Linear(N, N)
        self.lin3 = nn.Linear(N, 1)

    def forward(self, x):
        x = F.dropout(self.lin1(x), p=0.2, training=self.training)
        x = F.relu(x)
        x = F.dropout(self.lin2(x), p=0.2, training=self.training)
        x = F.relu(x)

        return F.sigmoid(self.lin3(x))


####################
# Utility functions
####################
def save_model(model, filename):
    print('Best model so far, saving it...')
    torch.save(model.state_dict(), filename)

def sample_categorical(batch_size, n_classes=10):
    '''
     Sample from a categorical distribution
     of size batch_size and # of classes n_classes
     return: torch.autograd.Variable with the sample
    '''
    cat = np.random.randint(0, 10, batch_size)
    cat = np.eye(n_classes)[cat].astype('float32')
    cat = torch.from_numpy(cat)
    return Variable(cat)


def report_loss(epoch, D_loss_cat, D_loss_gauss, G_loss, recon_loss):
    '''
    Print loss
    '''
    print('Epoch-{}; D_loss_cat: {:.4}: D_loss_gauss: {:.4}; G_loss: {:.4}; recon_loss: {:.4}'.format(epoch,
                                                                                                      D_loss_cat.data[0],
                                                                                                      D_loss_gauss.data[0],
                                                                                                      G_loss.data[0],
                                                                                                      recon_loss.data[0]))


def create_latent(Q, loader):
    '''
    Creates the latent representation for the samples in loader
    return:
        z_values: numpy array with the latent representations
        labels: the labels corresponding to the latent representations
    '''
    Q.eval()
    labels = []

    for batch_idx, (X, target) in enumerate(loader):

        X = X * 0.3081 + 0.1307
        # X.resize_(loader.batch_size, X_dim)
        X, target = Variable(X), Variable(target)
        labels.extend(target.data.tolist())
        if cuda:
            X, target = X.cuda(), target.cuda()
        # Reconstruction phase
        z_sample = Q(X)
        if batch_idx > 0:
            z_values = np.concatenate((z_values, np.array(z_sample.data.tolist())))
        else:
            z_values = np.array(z_sample.data.tolist())
    labels = np.array(labels)

    return z_values, labels


def classification_accuracy(Q, data_loader):
    Q.eval()
    labels = []
    test_loss = 0
    correct = 0

    for batch_idx, (X, target) in enumerate(data_loader):
        X = X * 0.3081 + 0.1307
        X.resize_(data_loader.batch_size, X_dim)
        X, target = Variable(X), Variable(target)
        if cuda:
            X, target = X.cuda(), target.cuda()

        labels.extend(target.data.tolist())
        # Reconstruction phase
        output = Q(X)[0]

        test_loss += F.nll_loss(output, target).data[0]

        pred = output.data.max(1)[1]
        correct += pred.eq(target.data).cpu().sum()

    test_loss /= len(data_loader)
    return 100. * correct / len(data_loader.dataset)

def newdataset():
    z_cat = np.arange(0, n_classes)
    z_cat = np.eye(n_classes)[z_cat].astype('float32')
    z_cat = torch.from_numpy(z_cat)
    z_cat = Variable(z_cat)
    if cuda:
        z_cat = z_cat.cuda()
    
    z_real_gauss = Variable(torch.randn(10, z_dim))
    if cuda:
        z_real_gauss = z_real_gauss.cuda()
    X_sample = P(torch.cat((z_cat, z_real_gauss),1))

    for i in range(5000):
        z_real_gauss = Variable(torch.randn(10, z_dim))
        if cuda:
            z_real_gauss = z_real_gauss.cuda()
        x_sample = P(torch.cat((z_cat, z_real_gauss),1))
        X_sample = torch.cat((x_sample, X_sample),0)

    xs = np.array(X_sample.data.tolist())

    pkl.dump(xs, open('./synthetic_samples', 'w'))

####################
# Train procedure
####################
def train(P, Q, D_cat, D_gauss, P_decoder, Q_encoder, Q_semi_supervised, Q_generator, D_cat_solver, D_gauss_solver, train_labeled_loader, train_unlabeled_loader):
    '''
    Train procedure for one epoch.
    '''
    TINY = 1e-15
    # Set the networks in train mode (apply dropout when needed)
    Q.train()
    P.train()
    D_cat.train()
    D_gauss.train()

    if train_unlabeled_loader is None:
        train_unlabeled_loader = train_labeled_loader

    # Loop through the labeled and unlabeled dataset getting one batch of samples from each
    # The batch size has to be a divisor of the size of the dataset or it will return
    # invalid samples
    for (X_l, target_l), (X_u, target_u) in itertools.izip(train_labeled_loader, train_unlabeled_loader):

        for X, target in [(X_u, target_u), (X_l, target_l)]:
            if target[0] == -1:
                labeled = False
            else:
                labeled = True

            # Load batch and normalize samples to be between 0 and 1
            X = X * 0.3081 + 0.1307
            X.resize_(train_batch_size, X_dim)

            X, target = Variable(X), Variable(target)
            if cuda:
                X, target = X.cuda(), target.cuda()

            # Init gradients
            P.zero_grad()
            Q.zero_grad()
            D_cat.zero_grad()
            D_gauss.zero_grad()

            #######################
            # Reconstruction phase
            #######################
            if not labeled:
                z_sample = torch.cat(Q(X), 1)
                X_sample = P(z_sample)

                recon_loss = F.binary_cross_entropy(X_sample + TINY, X.resize(train_batch_size, X_dim) + TINY)
                recon_loss = recon_loss
                recon_loss.backward()
                P_decoder.step()
                Q_encoder.step()

                P.zero_grad()
                Q.zero_grad()
                D_cat.zero_grad()
                D_gauss.zero_grad()
                recon_loss = recon_loss
                #######################
                # Regularization phase
                #######################
                # Discriminator
                Q.eval()
                z_real_cat = sample_categorical(train_batch_size, n_classes=n_classes)
                z_real_gauss = Variable(torch.randn(train_batch_size, z_dim))
                if cuda:
                    z_real_cat = z_real_cat.cuda()
                    z_real_gauss = z_real_gauss.cuda()

                z_fake_cat, z_fake_gauss = Q(X)

                D_real_cat = D_cat(z_real_cat)
                D_real_gauss = D_gauss(z_real_gauss)
                D_fake_cat = D_cat(z_fake_cat)
                D_fake_gauss = D_gauss(z_fake_gauss)

                D_loss_cat = -torch.mean(torch.log(D_real_cat + TINY) + torch.log(1 - D_fake_cat + TINY))
                D_loss_gauss = -torch.mean(torch.log(D_real_gauss + TINY) + torch.log(1 - D_fake_gauss + TINY))

                D_loss = D_loss_cat + D_loss_gauss
                D_loss = D_loss

                D_loss.backward()
                D_cat_solver.step()
                D_gauss_solver.step()

                P.zero_grad()
                Q.zero_grad()
                D_cat.zero_grad()
                D_gauss.zero_grad()

                # Generator
                Q.train()
                z_fake_cat, z_fake_gauss = Q(X)

                D_fake_cat = D_cat(z_fake_cat)
                D_fake_gauss = D_gauss(z_fake_gauss)

                G_loss = - torch.mean(torch.log(D_fake_cat + TINY)) - torch.mean(torch.log(D_fake_gauss + TINY))
                G_loss = G_loss
                G_loss.backward()
                Q_generator.step()

                P.zero_grad()
                Q.zero_grad()
                D_cat.zero_grad()
                D_gauss.zero_grad()

            #######################
            # Semi-supervised phase
            #######################
            if labeled:
                pred, _ = Q(X)
                class_loss = F.cross_entropy(pred, target)
                class_loss.backward()
                Q_semi_supervised.step()

                P.zero_grad()
                Q.zero_grad()
                D_cat.zero_grad()
                D_gauss.zero_grad()

    return D_loss_cat, D_loss_gauss, G_loss, recon_loss, class_loss


def generate_model(savefolder='./'):
    train_labeled_loader, train_unlabeled_loader, valid_loader = load_data()

    # Create and initialize Networks
    train_acc = []
    val_acc = []

    torch.manual_seed(10)

    if cuda:
        Q = Q_net().cuda()
        P = P_net().cuda()
        D_cat = D_net_cat().cuda()
        D_gauss = D_net_gauss().cuda()
    else:
        Q = Q_net()
        P = P_net()
        D_gauss = D_net_gauss()
        D_cat = D_net_cat()

    # Set learning rates
    gen_lr = 0.0006
    semi_lr = 0.001
    reg_lr = 0.0008

    # Set optimizators
    P_decoder = optim.Adam(P.parameters(), lr=gen_lr)
    Q_encoder = optim.Adam(Q.parameters(), lr=gen_lr)

    Q_semi_supervised = optim.Adam(Q.parameters(), lr=semi_lr)

    Q_generator = optim.Adam(Q.parameters(), lr=reg_lr)
    D_gauss_solver = optim.Adam(D_gauss.parameters(), lr=reg_lr)
    D_cat_solver = optim.Adam(D_cat.parameters(), lr=reg_lr)

    for epoch in range(epochs):
        if epoch == 75:
            P_decoder = optim.Adam(P.parameters(), lr=gen_lr / 10.)
            Q_encoder = optim.Adam(Q.parameters(), lr=gen_lr / 10.)

            Q_semi_supervised = optim.Adam(Q.parameters(), lr=semi_lr / 10.)

            Q_generator = optim.Adam(Q.parameters(), lr=reg_lr / 10.)
            D_gauss_solver = optim.Adam(D_gauss.parameters(), lr=reg_lr / 10.)
            D_cat_solver = optim.Adam(D_cat.parameters(), lr=reg_lr / 10.)

        D_loss_cat, D_loss_gauss, G_loss, recon_loss, class_loss = train(P, Q, D_cat,
                                                                         D_gauss, P_decoder,
                                                                         Q_encoder, Q_semi_supervised,
                                                                         Q_generator,
                                                                         D_cat_solver, D_gauss_solver,
                                                                         train_labeled_loader,
                                                                         train_unlabeled_loader)
        if epoch % 10 == 0:
            train_acc.append(classification_accuracy(Q, train_labeled_loader))
            val_acc.append(classification_accuracy(Q, valid_loader))
            report_loss(epoch, D_loss_cat, D_loss_gauss, G_loss, recon_loss)
            print('Classification Loss: {:.3}'.format(class_loss.data[0]))
            print('Validation accuracy: {} %'.format(val_acc[-1]))

    torch.save(Q.state_dict(), savefolder + 'Q_networkL')
    torch.save(P.state_dict(), savefolder + 'P_network')
    torch.save(D_cat.state_dict(), savefolder + 'D_cat_network')
    torch.save(D_gauss.state_dict(), savefolder + 'D_gauss_network')


if __name__ == '__main__':
    generate_model()







import argparse
import time
import torch
import pickle
import numpy as np
import itertools
from torch.autograd import Variable
import torch.nn as nn
import torch.nn.functional as F
import torch.autograd as autograd
import torch.optim as optim
from torch.nn.modules.upsampling import UpsamplingNearest2d

# Training settings
parser = argparse.ArgumentParser(description='PyTorch semi-supervised MNIST')

parser.add_argument('--batch-size', type=int, default=100, metavar='N',
                    help='input batch size for training (default: 100)')
parser.add_argument('--epochs', type=int, default=5000, metavar='N',
                    help='number of epochs to train (default: 10)')
parser.add_argument('--no-cuda', action='store_true', default=False,
                    help='enables CUDA training')


args = parser.parse_args()
args.cuda = not args.no_cuda and torch.cuda.is_available()

seed = 10
if args.cuda:
    torch.cuda.manual_seed(seed)
cuda = args.cuda

kwargs = {'num_workers': 1, 'pin_memory': True} if cuda else {}
n_classes = 10
z_dim = 10
X_dim = 784
y_dim = 10
convolutional = False
train_batch_size = args.batch_size
valid_batch_size = args.batch_size
N = 1000
epochs = args.epochs

##################################
# Load data and create Data loaders
##################################
def load_data(data_path='../data/'):
    print('loading data!')
    trainset_labeled = pickle.load(open(data_path + "train_labeled.p", "rb"))
    trainset_unlabeled = pickle.load(open(data_path + "train_unlabeled.p", "rb"))
    # Set -1 as labels for unlabeled data
    trainset_unlabeled.train_labels = torch.from_numpy(np.array([-1] * 47000))
    validset = pickle.load(open(data_path + "validation.p", "rb"))

    train_labeled_loader = torch.utils.data.DataLoader(trainset_labeled,
                                                       batch_size=train_batch_size,
                                                       shuffle=True, **kwargs)

    train_unlabeled_loader = torch.utils.data.DataLoader(trainset_unlabeled,
                                                         batch_size=train_batch_size,
                                                         shuffle=True, **kwargs)

    valid_loader = torch.utils.data.DataLoader(validset, batch_size=valid_batch_size, shuffle=True)

    return train_labeled_loader, train_unlabeled_loader, valid_loader

##################################
# Define Networks
##################################
# Encoder
class Q_net(nn.Module):
    def __init__(self):
        super(Q_net, self).__init__()
        self.lin1 = nn.Linear(X_dim, N)
        self.lin2 = nn.Linear(N, N)
        # Categorical code (y)
        self.lin3cat = nn.Linear(N, n_classes)
        # Gaussian code (z)
        self.lin3gauss = nn.Linear(N, z_dim)

    def forward(self, x):
        x = F.droppout(self.lin1(x), p=0.25, training=self.training)
        x = F.relu(x)
        x = F.droppout(self.lin2(x), p=0.25, training=self.training)
        x = F.relu(x)
        xcat = F.softmax(self.lin3cat(x))
        xgauss = self.lin3gauss(x)

        return xcat, xgauss

# Decoder
class P_net(nn.Module):
    def __init__(self):
        super(P_net, self).__init__()
        self.lin1 = nn.Linear(z_dim + n_classes, N)
        self.lin2 = nn.Linear(N, N)
        self.lin3 = nn.Linear(N, X_dim)

    def forward(self, x):
        x = self.lin1(x)
        x = F.dropout(x, p=0.25, training=self.training)
        x = F.relu(x)
        x = self.lin2(x)
        x = F.dropout(x, p=0.25, training=self.training)
        x = self.lin3(x)
        return F.sigmoid(x)


# Discriminator networks
class D_net_cat(nn.Module):
    def __init__(self):
        super(D_net_cat, self).__init__()
        self.lin1 = nn.Linear(n_classes, N)
        self.lin2 = nn.Linear(N, N)
        self.lin3 = nn.Linear(N, 1)

    def forward(self, x):
        x = F.dropout(self.lin1(x), p=0.2, training=self.training)
        x = F.relu(x)
        F.dropout(self.lin2(x), p=0.2, training=self.training)
        x = F.relu(x)
        return F.sigmoid(self.lin3(x))


class D_net_gauss(nn.Module):
    def __init__(self):
        super(D_net_gauss, self).__init__()
        self.lin1 = nn.Linear(z_dim, N)
        self.lin2 = nn.Linear(N, N)
        self.lin3 = nn.Linear(N, 1)

    def forward(self, x):
        x = F.dropout(self.lin1(x), p=0.2, training=self.training)
        x = F.relu(x)
        x = F.dropout(self.lin2(x), p=0.2, training=self.training)
        x = F.relu(x)

        return F.sigmoid(self.lin3(x))


####################
# Utility functions
####################
def save_model(model, filename):
    print('Best model so far, saving it...')
    torch.save(model.state_dict(), filename)

def sample_categorical(batch_size, n_classes=10):
    '''
     Sample from a categorical distribution
     of size batch_size and # of classes n_classes
     return: torch.autograd.Variable with the sample
    '''
    cat = np.random.randint(0, 10, batch_size)
    cat = np.eye(n_classes)[cat].astype('float32')
    cat = torch.from_numpy(cat)
    return Variable(cat)


def report_loss(epoch, D_loss_cat, D_loss_gauss, G_loss, recon_loss):
    '''
    Print loss
    '''
    print('Epoch-{}; D_loss_cat: {:.4}: D_loss_gauss: {:.4}; G_loss: {:.4}; recon_loss: {:.4}'.format(epoch,
                                                                                                      D_loss_cat.data[0],
                                                                                                      D_loss_gauss.data[0],
                                                                                                      G_loss.data[0],
                                                                                                      recon_loss.data[0]))


def create_latent(Q, loader):
    '''
    Creates the latent representation for the samples in loader
    return:
        z_values: numpy array with the latent representations
        labels: the labels corresponding to the latent representations
    '''
    Q.eval()
    labels = []

    for batch_idx, (X, target) in enumerate(loader):

        X = X * 0.3081 + 0.1307
        # X.resize_(loader.batch_size, X_dim)
        X, target = Variable(X), Variable(target)
        labels.extend(target.data.tolist())
        if cuda:
            X, target = X.cuda(), target.cuda()
        # Reconstruction phase
        z_sample = Q(X)
        if batch_idx > 0:
            z_values = np.concatenate((z_values, np.array(z_sample.data.tolist())))
        else:
            z_values = np.array(z_sample.data.tolist())
    labels = np.array(labels)

    return z_values, labels


def classification_accuracy(Q, data_loader):
    Q.eval()
    labels = []
    test_loss = 0
    correct = 0

    for batch_idx, (X, target) in enumerate(data_loader):
        X = X * 0.3081 + 0.1307
        X.resize_(data_loader.batch_size, X_dim)
        X, target = Variable(X), Variable(target)
        if cuda:
            X, target = X.cuda(), target.cuda()

        labels.extend(target.data.tolist())
        # Reconstruction phase
        output = Q(X)[0]

        test_loss += F.nll_loss(output, target).data[0]

        pred = output.data.max(1)[1]
        correct += pred.eq(target.data).cpu().sum()

    test_loss /= len(data_loader)
    return 100. * correct / len(data_loader.dataset)

def newdataset():
    z_cat = np.arange(0, n_classes)
    z_cat = np.eye(n_classes)[z_cat].astype('float32')
    z_cat = torch.from_numpy(z_cat)
    z_cat = Variable(z_cat)
    if cuda:
        z_cat = z_cat.cuda()
    
    z_real_gauss = Variable(torch.randn(10, z_dim))
    if cuda:
        z_real_gauss = z_real_gauss.cuda()
    X_sample = P(torch.cat((z_cat, z_real_gauss),1))

    for i in range(5000):
        z_real_gauss = Variable(torch.randn(10, z_dim))
        if cuda:
            z_real_gauss = z_real_gauss.cuda()
        x_sample = P(torch.cat((z_cat, z_real_gauss),1))
        X_sample = torch.cat((x_sample, X_sample),0)

    xs = np.array(X_sample.data.tolist())

    pkl.dump(xs, open('./synthetic_samples', 'w'))

####################
# Train procedure
####################
def train(P, Q, D_cat, D_gauss, P_decoder, Q_encoder, Q_semi_supervised, Q_generator, D_cat_solver, D_gauss_solver, train_labeled_loader, train_unlabeled_loader):
    '''
    Train procedure for one epoch.
    '''
    TINY = 1e-15
    # Set the networks in train mode (apply dropout when needed)
    Q.train()
    P.train()
    D_cat.train()
    D_gauss.train()

    if train_unlabeled_loader is None:
        train_unlabeled_loader = train_labeled_loader

    # Loop through the labeled and unlabeled dataset getting one batch of samples from each
    # The batch size has to be a divisor of the size of the dataset or it will return
    # invalid samples
    for (X_l, target_l), (X_u, target_u) in itertools.izip(train_labeled_loader, train_unlabeled_loader):

        for X, target in [(X_u, target_u), (X_l, target_l)]:
            if target[0] == -1:
                labeled = False
            else:
                labeled = True

            # Load batch and normalize samples to be between 0 and 1
            X = X * 0.3081 + 0.1307
            X.resize_(train_batch_size, X_dim)

            X, target = Variable(X), Variable(target)
            if cuda:
                X, target = X.cuda(), target.cuda()

            # Init gradients
            P.zero_grad()
            Q.zero_grad()
            D_cat.zero_grad()
            D_gauss.zero_grad()

            #######################
            # Reconstruction phase
            #######################
            if not labeled:
                z_sample = torch.cat(Q(X), 1)
                X_sample = P(z_sample)

                recon_loss = F.binary_cross_entropy(X_sample + TINY, X.resize(train_batch_size, X_dim) + TINY)
                recon_loss = recon_loss
                recon_loss.backward()
                P_decoder.step()
                Q_encoder.step()

                P.zero_grad()
                Q.zero_grad()
                D_cat.zero_grad()
                D_gauss.zero_grad()
                recon_loss = recon_loss
                #######################
                # Regularization phase
                #######################
                # Discriminator
                Q.eval()
                z_real_cat = sample_categorical(train_batch_size, n_classes=n_classes)
                z_real_gauss = Variable(torch.randn(train_batch_size, z_dim))
                if cuda:
                    z_real_cat = z_real_cat.cuda()
                    z_real_gauss = z_real_gauss.cuda()

                z_fake_cat, z_fake_gauss = Q(X)

                D_real_cat = D_cat(z_real_cat)
                D_real_gauss = D_gauss(z_real_gauss)
                D_fake_cat = D_cat(z_fake_cat)
                D_fake_gauss = D_gauss(z_fake_gauss)

                D_loss_cat = -torch.mean(torch.log(D_real_cat + TINY) + torch.log(1 - D_fake_cat + TINY))
                D_loss_gauss = -torch.mean(torch.log(D_real_gauss + TINY) + torch.log(1 - D_fake_gauss + TINY))

                D_loss = D_loss_cat + D_loss_gauss
                D_loss = D_loss

                D_loss.backward()
                D_cat_solver.step()
                D_gauss_solver.step()

                P.zero_grad()
                Q.zero_grad()
                D_cat.zero_grad()
                D_gauss.zero_grad()

                # Generator
                Q.train()
                z_fake_cat, z_fake_gauss = Q(X)

                D_fake_cat = D_cat(z_fake_cat)
                D_fake_gauss = D_gauss(z_fake_gauss)

                G_loss = - torch.mean(torch.log(D_fake_cat + TINY)) - torch.mean(torch.log(D_fake_gauss + TINY))
                G_loss = G_loss
                G_loss.backward()
                Q_generator.step()

                P.zero_grad()
                Q.zero_grad()
                D_cat.zero_grad()
                D_gauss.zero_grad()

            #######################
            # Semi-supervised phase
            #######################
            if labeled:
                pred, _ = Q(X)
                class_loss = F.cross_entropy(pred, target)
                class_loss.backward()
                Q_semi_supervised.step()

                P.zero_grad()
                Q.zero_grad()
                D_cat.zero_grad()
                D_gauss.zero_grad()

    return D_loss_cat, D_loss_gauss, G_loss, recon_loss, class_loss


def generate_model(savefolder='./'):
    train_labeled_loader, train_unlabeled_loader, valid_loader = load_data()

    # Create and initialize Networks
    train_acc = []
    val_acc = []

    torch.manual_seed(10)

    if cuda:
        Q = Q_net().cuda()
        P = P_net().cuda()
        D_cat = D_net_cat().cuda()
        D_gauss = D_net_gauss().cuda()
    else:
        Q = Q_net()
        P = P_net()
        D_gauss = D_net_gauss()
        D_cat = D_net_cat()

    # Set learning rates
    gen_lr = 0.0006
    semi_lr = 0.001
    reg_lr = 0.0008

    # Set optimizators
    P_decoder = optim.Adam(P.parameters(), lr=gen_lr)
    Q_encoder = optim.Adam(Q.parameters(), lr=gen_lr)

    Q_semi_supervised = optim.Adam(Q.parameters(), lr=semi_lr)

    Q_generator = optim.Adam(Q.parameters(), lr=reg_lr)
    D_gauss_solver = optim.Adam(D_gauss.parameters(), lr=reg_lr)
    D_cat_solver = optim.Adam(D_cat.parameters(), lr=reg_lr)

    for epoch in range(epochs):
        if epoch == 75:
            P_decoder = optim.Adam(P.parameters(), lr=gen_lr / 10.)
            Q_encoder = optim.Adam(Q.parameters(), lr=gen_lr / 10.)

            Q_semi_supervised = optim.Adam(Q.parameters(), lr=semi_lr / 10.)

            Q_generator = optim.Adam(Q.parameters(), lr=reg_lr / 10.)
            D_gauss_solver = optim.Adam(D_gauss.parameters(), lr=reg_lr / 10.)
            D_cat_solver = optim.Adam(D_cat.parameters(), lr=reg_lr / 10.)

        D_loss_cat, D_loss_gauss, G_loss, recon_loss, class_loss = train(P, Q, D_cat,
                                                                         D_gauss, P_decoder,
                                                                         Q_encoder, Q_semi_supervised,
                                                                         Q_generator,
                                                                         D_cat_solver, D_gauss_solver,
                                                                         train_labeled_loader,
                                                                         train_unlabeled_loader)
        if epoch % 10 == 0:
            train_acc.append(classification_accuracy(Q, train_labeled_loader))
            val_acc.append(classification_accuracy(Q, valid_loader))
            report_loss(epoch, D_loss_cat, D_loss_gauss, G_loss, recon_loss)
            print('Classification Loss: {:.3}'.format(class_loss.data[0]))
            print('Validation accuracy: {} %'.format(val_acc[-1]))

    torch.save(Q.state_dict(), savefolder + 'Q_networkL')
    torch.save(P.state_dict(), savefolder + 'P_network')
    torch.save(D_cat.state_dict(), savefolder + 'D_cat_network')
    torch.save(D_gauss.state_dict(), savefolder + 'D_gauss_network')


if __name__ == '__main__':
    generate_model()







import argparse
import time
import torch
import pickle
import numpy as np
import itertools
from torch.autograd import Variable
import torch.nn as nn
import torch.nn.functional as F
import torch.autograd as autograd
import torch.optim as optim
from torch.nn.modules.upsampling import UpsamplingNearest2d

# Training settings
parser = argparse.ArgumentParser(description='PyTorch semi-supervised MNIST')

parser.add_argument('--batch-size', type=int, default=100, metavar='N',
                    help='input batch size for training (default: 100)')
parser.add_argument('--epochs', type=int, default=5000, metavar='N',
                    help='number of epochs to train (default: 10)')
parser.add_argument('--no-cuda', action='store_true', default=False,
                    help='enables CUDA training')


args = parser.parse_args()
args.cuda = not args.no_cuda and torch.cuda.is_available()

seed = 10
if args.cuda:
    torch.cuda.manual_seed(seed)
cuda = args.cuda

kwargs = {'num_workers': 1, 'pin_memory': True} if cuda else {}
n_classes = 10
z_dim = 10
X_dim = 784
y_dim = 10
convolutional = False
train_batch_size = args.batch_size
valid_batch_size = args.batch_size
N = 1000
epochs = args.epochs

##################################
# Load data and create Data loaders
##################################
def load_data(data_path='../data/'):
    print('loading data!')
    trainset_labeled = pickle.load(open(data_path + "train_labeled.p", "rb"))
    trainset_unlabeled = pickle.load(open(data_path + "train_unlabeled.p", "rb"))
    # Set -1 as labels for unlabeled data
    trainset_unlabeled.train_labels = torch.from_numpy(np.array([-1] * 47000))
    validset = pickle.load(open(data_path + "validation.p", "rb"))

    train_labeled_loader = torch.utils.data.DataLoader(trainset_labeled,
                                                       batch_size=train_batch_size,
                                                       shuffle=True, **kwargs)

    train_unlabeled_loader = torch.utils.data.DataLoader(trainset_unlabeled,
                                                         batch_size=train_batch_size,
                                                         shuffle=True, **kwargs)

    valid_loader = torch.utils.data.DataLoader(validset, batch_size=valid_batch_size, shuffle=True)

    return train_labeled_loader, train_unlabeled_loader, valid_loader

##################################
# Define Networks
##################################
# Encoder
class Q_net(nn.Module):
    def __init__(self):
        super(Q_net, self).__init__()
        self.lin1 = nn.Linear(X_dim, N)
        self.lin2 = nn.Linear(N, N)
        # Categorical code (y)
        self.lin3cat = nn.Linear(N, n_classes)
        # Gaussian code (z)
        self.lin3gauss = nn.Linear(N, z_dim)

    def forward(self, x):
        x = F.droppout(self.lin1(x), p=0.25, training=self.training)
        x = F.relu(x)
        x = F.droppout(self.lin2(x), p=0.25, training=self.training)
        x = F.relu(x)
        xcat = F.softmax(self.lin3cat(x))
        xgauss = self.lin3gauss(x)

        return xcat, xgauss

# Decoder
class P_net(nn.Module):
    def __init__(self):
        super(P_net, self).__init__()
        self.lin1 = nn.Linear(z_dim + n_classes, N)
        self.lin2 = nn.Linear(N, N)
        self.lin3 = nn.Linear(N, X_dim)

    def forward(self, x):
        x = self.lin1(x)
        x = F.dropout(x, p=0.25, training=self.training)
        x = F.relu(x)
        x = self.lin2(x)
        x = F.dropout(x, p=0.25, training=self.training)
        x = self.lin3(x)
        return F.sigmoid(x)


# Discriminator networks
class D_net_cat(nn.Module):
    def __init__(self):
        super(D_net_cat, self).__init__()
        self.lin1 = nn.Linear(n_classes, N)
        self.lin2 = nn.Linear(N, N)
        self.lin3 = nn.Linear(N, 1)

    def forward(self, x):
        x = F.dropout(self.lin1(x), p=0.2, training=self.training)
        x = F.relu(x)
        F.dropout(self.lin2(x), p=0.2, training=self.training)
        x = F.relu(x)
        return F.sigmoid(self.lin3(x))


class D_net_gauss(nn.Module):
    def __init__(self):
        super(D_net_gauss, self).__init__()
        self.lin1 = nn.Linear(z_dim, N)
        self.lin2 = nn.Linear(N, N)
        self.lin3 = nn.Linear(N, 1)

    def forward(self, x):
        x = F.dropout(self.lin1(x), p=0.2, training=self.training)
        x = F.relu(x)
        x = F.dropout(self.lin2(x), p=0.2, training=self.training)
        x = F.relu(x)

        return F.sigmoid(self.lin3(x))


####################
# Utility functions
####################
def save_model(model, filename):
    print('Best model so far, saving it...')
    torch.save(model.state_dict(), filename)

def sample_categorical(batch_size, n_classes=10):
    '''
     Sample from a categorical distribution
     of size batch_size and # of classes n_classes
     return: torch.autograd.Variable with the sample
    '''
    cat = np.random.randint(0, 10, batch_size)
    cat = np.eye(n_classes)[cat].astype('float32')
    cat = torch.from_numpy(cat)
    return Variable(cat)


def report_loss(epoch, D_loss_cat, D_loss_gauss, G_loss, recon_loss):
    '''
    Print loss
    '''
    print('Epoch-{}; D_loss_cat: {:.4}: D_loss_gauss: {:.4}; G_loss: {:.4}; recon_loss: {:.4}'.format(epoch,
                                                                                                      D_loss_cat.data[0],
                                                                                                      D_loss_gauss.data[0],
                                                                                                      G_loss.data[0],
                                                                                                      recon_loss.data[0]))


def create_latent(Q, loader):
    '''
    Creates the latent representation for the samples in loader
    return:
        z_values: numpy array with the latent representations
        labels: the labels corresponding to the latent representations
    '''
    Q.eval()
    labels = []

    for batch_idx, (X, target) in enumerate(loader):

        X = X * 0.3081 + 0.1307
        # X.resize_(loader.batch_size, X_dim)
        X, target = Variable(X), Variable(target)
        labels.extend(target.data.tolist())
        if cuda:
            X, target = X.cuda(), target.cuda()
        # Reconstruction phase
        z_sample = Q(X)
        if batch_idx > 0:
            z_values = np.concatenate((z_values, np.array(z_sample.data.tolist())))
        else:
            z_values = np.array(z_sample.data.tolist())
    labels = np.array(labels)

    return z_values, labels


def classification_accuracy(Q, data_loader):
    Q.eval()
    labels = []
    test_loss = 0
    correct = 0

    for batch_idx, (X, target) in enumerate(data_loader):
        X = X * 0.3081 + 0.1307
        X.resize_(data_loader.batch_size, X_dim)
        X, target = Variable(X), Variable(target)
        if cuda:
            X, target = X.cuda(), target.cuda()

        labels.extend(target.data.tolist())
        # Reconstruction phase
        output = Q(X)[0]

        test_loss += F.nll_loss(output, target).data[0]

        pred = output.data.max(1)[1]
        correct += pred.eq(target.data).cpu().sum()

    test_loss /= len(data_loader)
    return 100. * correct / len(data_loader.dataset)

def newdataset():
    z_cat = np.arange(0, n_classes)
    z_cat = np.eye(n_classes)[z_cat].astype('float32')
    z_cat = torch.from_numpy(z_cat)
    z_cat = Variable(z_cat)
    if cuda:
        z_cat = z_cat.cuda()
    
    z_real_gauss = Variable(torch.randn(10, z_dim))
    if cuda:
        z_real_gauss = z_real_gauss.cuda()
    X_sample = P(torch.cat((z_cat, z_real_gauss),1))

    for i in range(5000):
        z_real_gauss = Variable(torch.randn(10, z_dim))
        if cuda:
            z_real_gauss = z_real_gauss.cuda()
        x_sample = P(torch.cat((z_cat, z_real_gauss),1))
        X_sample = torch.cat((x_sample, X_sample),0)

    xs = np.array(X_sample.data.tolist())

    pkl.dump(xs, open('./synthetic_samples', 'w'))

####################
# Train procedure
####################
def train(P, Q, D_cat, D_gauss, P_decoder, Q_encoder, Q_semi_supervised, Q_generator, D_cat_solver, D_gauss_solver, train_labeled_loader, train_unlabeled_loader):
    '''
    Train procedure for one epoch.
    '''
    TINY = 1e-15
    # Set the networks in train mode (apply dropout when needed)
    Q.train()
    P.train()
    D_gauss.train()

    # Loop through the data
    # The batch size has to be a divisor of the size of the dataset or it will return
    # invalid samples
    for X, target in data_loader:
        # Load batch and normalize samples to be between 0 and 1
        X = X * 0.3081 + 0.1307
        X.resize_(train_batch_size, X_dim)

        X, target = Variable(X), Variable(target)
        if cuda:
            X, target = X.cuda(), target.cuda()

        # Init gradients
        P.zero_grad()
        Q.zero_grad()
        D_gauss.zero_grad()

        #######################
        # Reconstruction phase
        #######################
        z_sample = Q(X)
        X_sample = P(z_sample)

        recon_loss = F.binary_cross_entropy(X_sample + TINY, X.resize(train_batch_size, X_dim) + TINY)

        recon_loss.backward()
        P_decoder.step()
        Q_encoder.step()

        P.zero_grad()
        Q.zero_grad()
        D_gauss.zero_grad()


        #######################
        # Regularization phase
        #######################
        # Discriminator
        Q.eval()    # Not use dropout in Q in this step
        z_real_gauss = Variable(torch.randn(train_batch_size, z_dim))

        if cuda:
            z_real_gauss = z_real_gauss.cuda()

        z_fake_gauss = Q(X)

        D_real_gauss = D_gauss(z_real_gauss)
        D_fake_gauss = D_gauss(z_fake_gauss)
        D_loss_gauss = -torch.mean(torch.log(D_real_gauss + TINY) + torch.log(1 - D_fake_gauss + TINY))

        D_loss.backward()
        D_gauss_solver.step()

        P.zero_grad()
        Q.zero_grad()
        D_gauss.zero_grad()

        # Generator
        Q.train()   # Back to use dropout
        z_fake_gauss = Q(X)
        D_fake_gauss = D_gauss(z_fake_gauss)

        G_loss = -torch.mean(torch.log(D_fake_gauss + TINY))
        G_loss.backward()
        Q_generator.step()

        P.zero_grad()
        Q.zero_grad()
        D_gauss.zero_grad()

    return D_loss_gauss, G_loss, recon_loss


def generate_model(savefolder='./'):
    train_labeled_loader, train_unlabeled_loader, valid_loader = load_data()

    # Create and initialize Networks
    train_acc = []
    val_acc = []

    torch.manual_seed(10)

    Q = Q_net()     # Encoder
    P = P_net()     # Decoder
    D_gauss = D_net_gauss()     # Discriminator adversarial
    if cuda:
        Q = Q.cuda()
        P = P.cuda()
        D_cat = D_gauss.cuda()
        D_gauss = D_net_gauss().cuda()

    # Set learning rates
    gen_lr = 0.0006
    reg_lr = 0.0008

    # Set optimizators
    P_decoder = optim.Adam(P.parameters(), lr=gen_lr)
    Q_encoder = optim.Adam(Q.parameters(), lr=gen_lr)

    Q_generator = optim.Adam(Q.parameters(), lr=reg_lr)
    D_gauss_solver = optim.Adam(D_gauss.parameters(), lr=reg_lr)

    for epoch in range(epochs):
        if epoch == 75:
            P_decoder = optim.Adam(P.parameters(), lr=gen_lr / 10.)
            Q_encoder = optim.Adam(Q.parameters(), lr=gen_lr / 10.)

            Q_semi_supervised = optim.Adam(Q.parameters(), lr=semi_lr / 10.)

            Q_generator = optim.Adam(Q.parameters(), lr=reg_lr / 10.)
            D_gauss_solver = optim.Adam(D_gauss.parameters(), lr=reg_lr / 10.)
            D_cat_solver = optim.Adam(D_cat.parameters(), lr=reg_lr / 10.)

        D_loss_cat, D_loss_gauss, G_loss, recon_loss, class_loss = train(P, Q, D_cat,
                                                                         D_gauss, P_decoder,
                                                                         Q_encoder, Q_semi_supervised,
                                                                         Q_generator,
                                                                         D_cat_solver, D_gauss_solver,
                                                                         train_labeled_loader,
                                                                         train_unlabeled_loader)
        if epoch % 10 == 0:
            train_acc.append(classification_accuracy(Q, train_labeled_loader))
            val_acc.append(classification_accuracy(Q, valid_loader))
            report_loss(epoch, D_loss_cat, D_loss_gauss, G_loss, recon_loss)
            print('Classification Loss: {:.3}'.format(class_loss.data[0]))
            print('Validation accuracy: {} %'.format(val_acc[-1]))

    torch.save(Q.state_dict(), savefolder + 'Q_networkL')
    torch.save(P.state_dict(), savefolder + 'P_network')
    torch.save(D_cat.state_dict(), savefolder + 'D_cat_network')
    torch.save(D_gauss.state_dict(), savefolder + 'D_gauss_network')


if __name__ == '__main__':
    generate_model()








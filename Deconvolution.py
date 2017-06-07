from AlexNet import AlexNet, AlexNetModel, preprocess_image_batch
from Deconv_AdditionalLayers import subtract_bias

from keras.models import Model
from keras.layers import Input, Conv2DTranspose
from keras.utils.layer_utils import convert_all_kernels_in_model
from keras import backend as K
from keras.optimizers import SGD

from PIL import Image
import numpy as np
import os


class Deconvolution:
    channels = [3, 96, 256, 384, 384, 256]  # Corresponds to the number of filters in convolution

    def __init__(self, conv_base_model=None):
        # Set convolutional model and submodels, which get activations after given layer
        self.conv_base_model = conv_base_model if conv_base_model else AlexNetModel()
        self.conv_sub_models = [None] + [AlexNet(i, self.conv_base_model) for i in (1, 2, 3, 4, 5)]  # Make it 1-based

        # Get deconvolutional layers from Deconv_Layers instance
        DeconvLayers_Instance = DeconvLayers(self.conv_base_model)
        self.deconv_layers = DeconvLayers_Instance.deconv_layers
        self.bias3D = DeconvLayers_Instance.bias3D

        # This attributes will be filled by 'project_down' method
        self.array = None  # Tensor being projected down from feature space to image space
        self.activation_maxpool = None  # Activation for max_pool layer 1 and 2, needed for switches
        self.current_layer = None  # Changes as array is passed on
        self.f = None  # Filter whose activation is projected down

    def project_down(self, image_path, layer, f=None):
        assert type(layer) == int
        self.current_layer = layer
        self.f = f
        self.array = self.conv_sub_models[self.current_layer].predict(image_path)
        self.activation_maxpool = [None] + [self.conv_sub_models[i].predict(image_path) for i in (1, 2)]

        if f:
            self.set_zero_except_maximum()

        if self.current_layer >= 5:
            self.project_through_split_convolution()
            # Zerounpadding
            self.array = self.array[:, :, 1:-1, 1:-1]
        if self.current_layer >= 4:
            self.project_through_split_convolution()
            # Zerounpadding
            self.array = self.array[:, :, 1:-1, 1:-1]
        if self.current_layer >= 3:
            self.project_through_convolution()
            # Zerounpadding
            self.array = self.array[:, :, 1:-1, 1:-1]
        if self.current_layer >= 2:
            self.unpool()
            self.project_through_split_convolution()
            # Zerounpadding
            self.array = self.array[:, :, 2:-2, 2:-2]
        if self.current_layer >= 1:
            self.unpool()
            self.project_through_convolution()
        return self.array

    def zero_un_padd(self):
        # TODO: Implement as method
        pass

    def project_through_convolution(self):
        cl = self.current_layer
        assert cl in (1, 3)

        deconv_cl = 'deconv_{}'.format(cl)
        self.array = self.deconv_layers[deconv_cl].predict(self.array)
        self.current_layer -= 1

    def project_through_split_convolution(self):
        cl = self.current_layer
        assert cl in (2, 4, 5)

        # Make sure dimensions are fine
        assert self.array.shape[1] == self.channels[cl], 'Channel number incorrect'

        # Split, perform Deconvolution on splits, merge
        activation_cl_1 = self.array[:, : self.channels[cl] // 2]
        activation_cl_2 = self.array[:, self.channels[cl] // 2:]

        deconv_cl_1 = 'deconv_{}_1'.format(cl)
        deconv_cl_2 = 'deconv_{}_2'.format(cl)
        projected_activation_cl_1 = self.deconv_layers[deconv_cl_1].predict(activation_cl_1)
        projected_activation_cl_2 = self.deconv_layers[deconv_cl_2].predict(activation_cl_2)

        self.array = np.concatenate((projected_activation_cl_1, projected_activation_cl_2), axis=1)
        assert self.array.shape[1] == self.channels[cl - 1], 'Channel number incorrect'
        self.current_layer -= 1

    def unpool(self):
        cl = self.current_layer
        assert cl in (1, 2), 'Maxpooling only for layer one and two'
        activations = self.activation_maxpool[cl]

        # Network parameters for maxpool layers
        kernel = 3
        stride = 2

        # TODO: Simplify to last to lines
        # Change last to lines to assignment once everything works nicely
        assert cl in (1, 2)
        if cl == 1:
            input_shape = (96, 55, 55)
            output_shape = (96, 27, 27)
        if cl == 2:
            input_shape = (256, 27, 27)
            output_shape = (256, 13, 13)
        assert activations.shape[1:] == input_shape
        assert self.array.shape[1:] == output_shape

        reconstructed_activations = np.zeros_like(activations)
        for f in range(output_shape[0]):
            for i_out in range(output_shape[1]):
                for j_out in range(output_shape[2]):
                    i_in, j_in = i_out * stride, j_out * stride
                    sub_square = activations[0, f, i_in:i_in + kernel, j_in:j_in + kernel]
                    max_pos_i, max_pos_j = np.unravel_index(np.nanargmax(sub_square), (kernel, kernel))
                    array_pixel = self.array[0, f, i_out, j_out]

                    # Since poolings are overlapping, two activations might be reconstructed to same spot
                    # Keep the higher activation
                    if reconstructed_activations[0, f, i_in + max_pos_i, j_in + max_pos_j] < array_pixel:
                        reconstructed_activations[0, f, i_in + max_pos_i, j_in + max_pos_j] = array_pixel
        self.array = reconstructed_activations

    def set_zero_except_maximum(self):
        # Set other layers to zero
        new_array = np.zeros_like(self.array)
        new_array[0, self.f - 1] = self.array[0, self.f - 1]

        # Set other activations in same layer to zero
        max_index_flat = np.nanargmax(new_array)
        max_index = np.unravel_index(max_index_flat, new_array.shape)
        self.array = np.zeros_like(new_array)
        self.array[max_index] = new_array[max_index]


class DeconvLayers:
    conv_layer_names = ['conv_' + id for id in ('1', '2_1', '2_2', '3', '4_1', '4_2', '5_1', '5_2')]
    deconv_layer_names = ['deconv_' + id for id in ('1', '2_1', '2_2', '3', '4_1', '4_2', '5_1', '5_2')]

    def __init__(self, conv_base_model):

        # Full AlexNet
        self.conv_base_model = conv_base_model
        self.conv_layers = self.init_conv_layers()

        self.weights = self.init_weights_dict()
        self.bias3D = self.init_bias3D_dict()

        self.deconv_layers = self.init_deconv_layers()

    def Deconv_Layer_Model(self, layer_name):
        K.set_image_dim_ordering('th')

        # Get local variables from dictionaries
        conv_layer = self.conv_layers[layer_name]
        w, b = self.weights[layer_name].tuple

        # Get deconvolutional shapes from convolutional shapes
        deconv_from_shape = conv_layer.output_shape[1:]
        deconv_to_shape = conv_layer.input_shape[1:]
        de_layer_name = 'de' + layer_name

        # Build layer
        inputs = Input(shape=deconv_from_shape)
        prediction = Conv2DTranspose(filters=deconv_to_shape[0],  # N_Filters equals N_channels of convolution input
                                     kernel_size=conv_layer.kernel_size,
                                     strides=conv_layer.strides,
                                     activation='relu',
                                     use_bias=False,
                                     name=de_layer_name)(inputs)

        m = Model(input=inputs, output=prediction)
        convert_all_kernels_in_model(m)

        sgd = SGD(lr=0.1, decay=1e-6, momentum=0.9, nesterov=True)
        m.compile(optimizer=sgd, loss='mse')

        assert m.get_layer(de_layer_name).get_weights()[0].shape == w.shape
        m.get_layer(de_layer_name).set_weights([w])

        return m

    def init_conv_layers(self):
        conv_layer_dict = {}
        for layer_name in self.conv_layer_names:
            conv_layer_dict[layer_name] = self.conv_base_model.get_layer(layer_name)
        return conv_layer_dict

    def init_weights_dict(self):
        weights_dict = {}
        for layer_name, layer in self.conv_layers.items():
            w = layer.get_weights()[0]
            b = layer.get_weights()[1]
            weights_dict[layer_name] = W(w, b)
        return weights_dict

    def init_bias3D_dict(self):
        bias_dict = {}
        for layer_name, layer in self.conv_layers.items():
            b3D = np.zeros(layer.output_shape[1:])
            for f in range(b3D.shape[0]):
                b3D[f] = np.full_like(b3D[0], self.weights[layer_name].b[f])
            b3D = np.expand_dims(b3D, axis=0)
            bias_dict[layer_name] = b3D
        return bias_dict

    def init_deconv_layers(self):
        deconv_layers = {}
        for deconv_layer_name in self.deconv_layer_names:
            deconv_layers[deconv_layer_name] = self.Deconv_Layer_Model(deconv_layer_name[2:])
        return deconv_layers


class W:
    def __init__(self, w, b):
        assert type(w) == np.ndarray
        assert type(b) == np.ndarray
        self.w = w
        self.b = b
        self.tuple = (w, b)


class Deconv_Output:
    def __init__(self, unarranged_array):  # Takes output of DeconvNet
        self.array = self._rearrange_array(unarranged_array)
        self.image = None

    def _rearrange_array(self, unarranged_array):
        assert len(unarranged_array.shape) in (3, 4)

        # If Array is not yet rearranged
        if len(unarranged_array.shape) == 4:
            assert unarranged_array.shape[0] == 1
            unarranged_array = unarranged_array[0, :, :, :]  # Eliminate batch size dimension
            unarranged_array = np.moveaxis(unarranged_array, 0, -1)  # Put channels last
            # Undo sample mean subtraction
            unarranged_array[:, :, 0] += 123.68
            unarranged_array[:, :, 1] += 116.779
            unarranged_array[:, :, 2] += 103.939

        return unarranged_array

    def save_as(self, folder=None, filename='test.JPEG'):
        self.image = Image.fromarray(self.array.astype(np.uint8), 'RGB')
        if self.image.mode != 'RGB':
            self.image = self.image.convert('RGB')

        if folder is not None:
            assert type(folder) is str
            filename = folder + '/' + filename

        try:
            os.remove(filename)
        except OSError:
            pass

        self.image.save(filename)


def visualize_all_filter_in_layer1(conv_model):
    w = conv_model.get_layer('conv_1').get_weights()[0]
    for f in range(96):
        wf = w[:, :, :, f - 1]
        # scale = min(abs(100/wf.max()),abs(100/wf.min()))
        scale = 1000
        wf *= scale
        wf[:, :, 0] += 123.68
        wf[:, :, 1] += 116.779
        wf[:, :, 2] += 103.939
        result = Deconv_Output(wf)
        result.save_as(filename='Filters_Layer1_Visualized/filter{}.JPEG'.format(f + 1))


if __name__ == '__main__':
    layer = 5
    f = 10
    img_path = 'Layer{}_Strongest_IMG/Layer{}_Filter{}_Top1.JPEG'.format(layer,layer,f)
    array = Deconvolution().project_down(img_path, layer=5, f=1)
    Deconv_Output(array).save_as()

    # Filter visualization and image output test
    if False:
        # Visualize first layer filters
        conv_model = AlexNetModel()
        visualize_all_filter_in_layer1(conv_model)

        # Show that inverting preprocessing works
        img_path = 'Example_JPG/Elephant.jpg'
        array = preprocess_image_batch([img_path])
        Deconv_Output(array).save_as(filename='array.JPEG')


        # activation_of_one_filter = np.zeros_like(activations)
        # activation_of_one_filter[:, f - 1, :, :] = activations[:, f - 1, :, :]
        # deconv = Deconvolution(conv_base_model)
        # deconv.project_back(activations)
        # result = Deconv_Output(deconv.project_back(activations))
        # result.save_as()


        # conv_base_model = AlexNet()
        # conv_model = Model(inputs=conv_base_model.input, outputs=conv_base_model.get_layer('conv_1').output)
        # deconv_model = Deconv_Net(conv_base_model)
        # # deconv_model.summary()
        # im = preprocess_image_batch(['Layer1_Strongest_IMG/Layer1_Filter{}_Top7.JPEG'.format(filter)])
        # activation = conv_model.predict(im)
        # # filter = 1
        # # activation_of_one_filter = np.zeros_like(activation)
        # # activation_of_one_filter[:, filter - 1, :, :] = activation[:, filter - 1, :, :]
        # # # activation_of_one_filter = activation
        # # max_activation_of_one_filter = np.zeros_like(activation)
        # # max_loc = activation_of_one_filter.argmax()
        # # max_loc = np.unravel_index(max_loc,activation.shape)
        # # max_activation_of_one_filter[0+max_loc] = activation_of_one_filter.max()
        # result = deconv_model.predict(activation)

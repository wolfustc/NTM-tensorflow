from __future__ import division, print_function, absolute_import
from functools import reduce

import numpy as np
import tensorflow as tf

from utils import *
from layers import *

class NTM(object):
    def __init__(self, config=None, sess=None):
        self.sess = sess

        self.input_dim = 128
        self.output_dim = 128
        self.mem_size = 128
        self.mem_dim = 20
        self.controller_dim = 100
        self.controller_layer_size = 2
        self.shift_range = 1
        self.write_head_size = 1
        self.read_head_size = 1

        #self.input_dim = config.input_dim or 128
        #self.output_dim = config.output_dim or 128
        #self.mem_size = config.mem_size or 128
        #self.mem_dim = config.mem_dim or 20
        #self.controller_dim = config.controller_dim    or 100
        #self.controller_layer_size = config.controller_layer_size or 1
        #self.shift_range = config.shift_range or 1
        #self.write_head_size = config.write_head_size or 1
        #self.read_heads_size = config.read_heads_size  or 1

        self.depth = 0
        self.input_cells = []
        self.output_cells = []
        self.prev_outputs = []

        with tf.variable_scope("cell") as scope:
            self.master_input_cell, self.master_output_cell = self.build_cell()
        self.init_input_cell, self.init_output_cell = self.build_init_cell()

        print(" [*] Initialization start...")
        self.sess.run(tf.initialize_all_variables())
        print(" [*] Initialization end")

    def build_init_cell(self):
        with tf.variable_scope("init_cell"):
            # always zero
            dummy = tf.placeholder(tf.float32, [1, 1])

            # memory
            M_init_linear = tf.tanh(Linear(dummy, self.mem_size * self.mem_dim, bias=True))
            M_init = tf.reshape(M_init_linear, [self.mem_size, self.mem_dim])

            # read weights
            read_w_init = tf.Variable(tf.zeros([self.read_head_size, self.mem_size]))
            read_init = tf.Variable(tf.zeros([self.read_head_size, 1, self.mem_dim]))

            for idx in xrange(self.read_head_size):
                # initialize bias distribution with `tf.range(mem_size-2, 0, -1)`
                read_w_linear_idx = Linear(dummy, self.mem_size, is_range=True)
                read_w_init = tf.scatter_update(read_w_init, [idx], tf.nn.softmax(read_w_linear_idx))

                read_init_idx = tf.tanh(Linear(dummy, self.mem_dim))
                read_init = tf.scatter_update(read_init, [idx], tf.reshape(read_init_idx, [1, 1, self.mem_dim]))

            # write weights
            write_w_init = tf.Variable(tf.zeros([self.write_head_size, self.mem_size]))
            for idx in xrange(self.write_head_size):
                write_w_linear_idx = Linear(dummy, self.mem_size)
                write_w_init = tf.scatter_update(write_w_init, [idx], tf.nn.softmax(write_w_linear_idx))

            # controller state
            output_init = tf.Variable(tf.zeros([self.controller_layer_size, self.output_dim]))
            hidden_init = tf.Variable(tf.zeros([self.controller_layer_size, self.controller_dim]))

            for idx in xrange(self.controller_layer_size):
                hidden_init = tf.scatter_update(hidden_init, [idx], tf.reshape(
                        tf.tanh(Linear(dummy, self.controller_dim)), [1, self.controller_dim]
                    )
                )
                output_init = tf.scatter_update(output_init, [idx], tf.reshape(
                        tf.tanh(Linear(dummy, self.output_dim)), [1, self.output_dim]
                    )
                )

            new_output= tf.tanh(Linear(dummy, self.output_dim, bias=True, bias_init=1))

            inputs = {
                'input': dummy,
            }
            outputs = {
                'new_output': new_output,
                'M': M_init,
                'read_w': read_w_init,
                'write_w': write_w_init,
                'read': tf.reshape(read_init, [self.read_head_size, self.mem_dim]),
                'output': output_init,
                'hidden': hidden_init
            }
            return inputs, outputs

    # Build a NTM cell which shares weights with "master" cell.
    def build_cell(self):
        input = tf.placeholder(tf.float32, [1, self.input_dim], name='build_cell_input')

        # previous memory state
        M_prev = tf.placeholder(tf.float32, [self.mem_size, self.mem_dim], name='build_cell_M_prev')

        # previous read/write weights
        read_w_prev = tf.placeholder(tf.float32, [self.read_head_size, self.mem_size])
        write_w_prev = tf.placeholder(tf.float32, [self.write_head_size, self.mem_size])

        # previous vector read from memory
        read_prev = tf.placeholder(tf.float32, [1, self.mem_dim])

        # previous LSTM controller output
        output_prev = tf.placeholder(tf.float32, [self.controller_layer_size, self.output_dim])
        hidden_prev = tf.placeholder(tf.float32, [self.controller_layer_size, self.controller_dim])

        # output and hidden states of controller module
        output, hidden = self.build_controller(input, read_prev, output_prev, hidden_prev)

        # last output layer from LSTM controller
        last_output = output if self.controller_layer_size == 1 else output[-1]

        # Build a memory
        M, read_w, write_w, read = self.build_memory(M_prev, read_w_prev, write_w_prev, last_output)
        new_output = self.build_output(last_output)

        inputs = {
            'input': input,
            'M_prev': M_prev,
            'read_w_prev': read_w_prev,
            'write_w_prev': write_w_prev,
            'read_prev': read_prev,
            'output_prev': output_prev,
            'hidden_prev': hidden_prev
        }
        outputs = {
            'new_output': new_output,
            'M': M,
            'read_w': read_w,
            'write_w': write_w,
            'read': read,
            'output': output,
            'hidden': hidden,
        }
        return inputs, outputs

    def build_read_head(self, M_prev, read_w_prev, last_output):
        return self.build_head(M_prev, read_w_prev, last_output, True)

    def build_write_head(self, M_prev, write_w_prev, last_output):
        return self.build_head(M_prev, write_w_prev, last_output, False)

    def build_output(self, output):
        return tf.sigmoid(Linear(output, self.output_dim))

    def build_head(self, M_prev, w_prev, last_output, is_read):
        scope = 'read' if is_read else 'write'

        with tf.variable_scope(scope):
            # Figure 2.
            # Amplify or attenuate the precision
            with tf.variable_scope("k"):
                k = tf.tanh(Linear(last_output, self.mem_dim))
            # Interpolation gate
            with tf.variable_scope("g"):
                g = tf.sigmoid(Linear(last_output, 1))
            # shift weighting
            with tf.variable_scope("s_w"):
                s_w = tf.reshape(tf.nn.softmax(Linear(last_output, 2 * self.shift_range + 1)), [-1, 1])
            with tf.variable_scope("beta"):
                beta  = tf.nn.softplus(Linear(last_output, 1))
            with tf.variable_scope("gamma"):
                gamma = tf.add(tf.nn.softplus(Linear(last_output, 1)), tf.constant(1.0))

            # 3.3.1
            # Cosine similarity
            similarity = SmoothCosineSimilarity(M_prev, k) # [mem_size x 1]
            # Focusing by content
            content_focused_w = tf.nn.softmax(ScalarMul(similarity, beta))

            # 3.3.2
            # Focusing by location
            gated_w = tf.add_n([
                ScalarMul(content_focused_w, g),
                ScalarMul((tf.constant(1.0) - g), w_prev)
            ])

            # Convolutional shifts
            conv_w = CircularConvolution(vector=gated_w, kernel=s_w)

            # Sharpening
            powed_conv_w = tf.pow(conv_w, gamma)
            w = powed_conv_w / tf.reduce_sum(powed_conv_w)

            if is_read:
                # 3.1 Reading
                read = M_prev * w
                return w, read
            else:
                # 3.2 Writing
                erase = tf.sigmoid(Linear(last_output, self.mem_dim)) # [1 x mem_dim]
                add = tf.tanh(Linear(last_output, self.mem_dim))
                return w, add, erase

    # build a memory to read & write
    def build_memory(self, M_prev, read_w_prev, write_w_prev, last_output):
        with tf.variable_scope("memory"):
            # 3.1 Reading
            if self.read_head_size == 1:
                read_w, read = self.build_read_head(M_prev, tf.reshape(read_w_prev, [-1, 1]), last_output)
            else:
                read_w = tf.Variable(tf.zeros([self.read_head_size, self.mem_size]))
                read = tf.Variable(tf.zeros([self.read_head_size, self.mem_size, self.mem_dim]))

                for idx in xrange(self.read_head_size):
                    read_w_prev_idx = tf.reshape(tf.gather(read_w_prev, idx), [-1, 1])

                    read_w_idx, read_idx = self.build_read_head(M_prev, read_w_prev_idx, last_output)

                    read_w = tf.scatter_update(read_w, [idx], tf.transpose(read_w_idx))
                    read = tf.scatter_update(read, [idx], tf.reshape(read_idx, [1, self.mem_size, self.mem_dim]))

            # 3.2 Writing
            if self.write_head_size == 1:
                write_w, write, erase = self.build_write_head(M_prev, tf.reshape(write_w_prev, [-1, 1]), last_output)

                M_erase = tf.ones([self.mem_size, self.mem_dim]) - OuterProd(write_w, erase)
                M_write = OuterProd(write_w, write)
            else:
                write_w = tf.Variable(tf.zeros([self.write_head_size, self.mem_size]))
                write = tf.Variable(tf.zeros([self.write_head_size, self.mem_size, self.mem_dim]))
                erase = tf.Variable(tf.zeros([self.write_head_size, 1, self.mem_dim]))

                M_erases = []
                M_writes = []

                for idx in xrange(self.write_head_size):
                    write_w_prev_idx = tf.reshape(tf.gather(write_w_prev, idx), [-1, 1])

                    write_w_idx, write_idx, erase_idx = self.build_write_head(M_prev, write_w_prev_idx, last_output)

                    write_w = tf.scatter_update(write_w, [idx], tf.transpose(write_w_idx))
                    write = tf.scatter_update(write, [idx], tf.reshape(write_idx, [1, self.mem_size, self.mem_dim]))
                    erase = tf.scatter_update(erase, [idx], tf.reshape(erase_idx, [1, 1, self.mem_dim]))

                    M_erases.append(tf.ones([self.mem_size, self.mem_dim]) * OuterProd(write_w_idx, erase_idx))
                    M_writes.append(OuterProd(write_w_idx, write_idx))

                M_erase = reduce(lambda x, y: x*y, M_erases)
                M_write = tf.add_n(M_writes)

            M = M_prev * M_erase + M_write

            return M, read_w, write_w, read

    # Build a LSTM controller
    def build_controller(self, input, read_prev, output_prev, hidden_prev):
        with tf.variable_scope("controller"):
            output, hidden = [], []
            for layer_idx in xrange(self.controller_layer_size):
                if self.controller_layer_size == 1:
                    o_prev = output_prev
                    h_prev = hidden_prev
                else:
                    o_prev = tf.reshape(tf.gather(output_prev, layer_idx), [1, -1])
                    h_prev = tf.reshape(tf.gather(hidden_prev, layer_idx), [1, -1])

                if layer_idx == 0:
                    def new_gate():
                        in_modules = [
                            Linear(input, self.controller_dim),
                            Linear(o_prev, self.controller_dim),
                        ]
                        if self.read_head_size == 1:
                            in_modules.append(
                                Linear(read_prev, self.controller_dim)
                            )
                        else:
                            for read_idx in xrange(self.read_head_size):
                                vec = tf.reshape(tf.gather(read_prev, read_idx), [1, -1])
                                in_modules.append(
                                    Linear(vec, self.controller_dim)
                                )
                        return tf.add_n(in_modules)
                else:
                    def new_gate():
                        return tf.add_n([
                            Linear(output[layer_idx-1], self.controller_dim),
                            Linear(o_prev, self.controller_dim),
                        ])

                # input, forget, and output gates for LSTM
                i = tf.sigmoid(new_gate())
                f = tf.sigmoid(new_gate())
                o = tf.sigmoid(new_gate())
                update = tf.tanh(new_gate())

                # update the sate of the LSTM cell
                hidden.append(tf.add_n([f * h_prev, i * update]))
                output.append(o * tf.tanh(hidden[layer_idx]))

            return output, hidden

    def forward(self, input):
        self.depth += 1
        
        if self.depth == 1:
            prev_outputs = self.sess.run([
                    self.init_output_cell['new_output'],
                    self.init_output_cell['M'], self.init_output_cell['read_w'],
                    self.init_output_cell['write_w'], self.init_output_cell['read'],
                    self.init_output_cell['output'], self.init_output_cell['hidden']
                ], feed_dict={
                    self.init_input_cell['input']: [[0.0]]
                }
            )
        else:
            prev_outputs = self.prev_outputs[self.depth - 1]

        try:
            cur_input_cell = self.input_cells[self.depth]
            cur_output_cell = self.output_cells[self.depth]
        except:
            with tf.variable_scope("cell", reuse=True):
                cur_input_cell, cur_output_cell = self.build_cell()
                #self.sess.run(tf.initialize_all_variables())
                self.input_cells.append(cur_input_cell)
                self.output_cells.append(cur_output_cell)

        outputs = self.sess.run([
                cur_output_cell['new_output'],
                cur_output_cell['M'], cur_output_cell['read_w'], cur_output_cell['write_w'],
                cur_output_cell['read'], cur_output_cell['output'][-1], cur_output_cell['hidden'][-1]
            ], feed_dict = {
                cur_input_cell['input']: input,
                cur_input_cell['M_prev']: prev_outputs[1],
                cur_input_cell['read_w_prev']: prev_outputs[2],
                cur_input_cell['write_w_prev']: prev_outputs[3],
                cur_input_cell['read_prev']: prev_outputs[4],
                cur_input_cell['output_prev']: prev_outputs[5],
                cur_input_cell['hidden_prev']: prev_outputs[6]
            }
        )
        self.output = outputs[0]
        self.prev_outputs = outputs

        return self.output

    def get_memory(self, depth=None):
        if self.depth == 0:
            inputs, outputs = self.init_cell()
            return outputs['M_init']
        #depth = depth if depth or self.depth
        return self.cells[depth]

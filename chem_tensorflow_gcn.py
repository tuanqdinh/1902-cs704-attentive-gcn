#!/usr/bin/env/python
'''
Usage:
    chem_tensorflow_gcn.py [options]

Options:
    -h --help                Show this screen.
    --config-file FILE       Hyperparameter configuration file path (in JSON format)
    --config CONFIG          Hyperparameter configuration dictionary (in JSON format)
    --log_dir NAME           log dir name
    --data_dir NAME          data dir name
    --restore FILE           File to restore weights from.
    --freeze-graph-model     Freeze weights of graph model components.
    --evaluate               example evaluation mode using a restored model
'''
from typing import Tuple, Sequence, Any

from docopt import docopt
import numpy as np
import tensorflow as tf
from collections import defaultdict

import sys, traceback
import pdb
import json

from chem_tensorflow import ChemModel
from utils import glorot_init

def graph_to_adj_mat(graph, max_n_vertices, num_edge_types, tie_fwd_bkwd=True):
    bwd_edge_offset = 0 if tie_fwd_bkwd else (num_edge_types // 2)
    amat = np.zeros((num_edge_types, max_n_vertices, max_n_vertices))
    for src, e, dest in graph:
        amat[e-1, dest, src] = 1
        amat[e-1 + bwd_edge_offset, src, dest] = 1
    return amat

class SparseGCNChemModel(ChemModel):
    def __init__(self, args):
        super().__init__(args)

    @classmethod
    def default_params(cls):
        params = dict(super().default_params())
        params.update({'batch_size': 100000,
                       'task_sample_ratios': {},
                       'gcn_use_bias': False,
                       'graph_state_dropout_keep_prob': 1.0,
                       })
        return params

    ## OVERWRITE
    def prepare_specific_graph_model(self) -> None:
        h_dim = self.params['hidden_size']
        self.placeholders['initial_node_representation'] = tf.placeholder(tf.float32, [None, h_dim], name='node_features')
        self.placeholders['adjacency_list'] = tf.placeholder(tf.int64, [None, 2], name='adjacency_list')
        self.placeholders['adjacency_weights'] = tf.placeholder(tf.float32, [None], name='adjacency_weights')
        self.placeholders['graph_nodes_list'] = tf.placeholder(tf.int32, [None], name='graph_nodes_list')
        self.placeholders['graph_state_keep_prob'] = tf.placeholder(tf.float32, None, name='graph_state_keep_prob')

        with tf.variable_scope('gcn_scope'):
            self.weights['edge_weights'] = [tf.Variable(glorot_init((h_dim, h_dim)), name="gcn_weights_%i" % i) for i in range(self.params['num_timesteps'])]

            if self.params['gcn_use_bias']:
                self.weights['edge_biases'] = [tf.Variable(np.zeros([h_dim], dtype=np.float32), name="gcn_bias_%i" % i) for i in range(self.params['num_timesteps'])]

    ## OVERWRITE
    def compute_final_node_representations(self):
        with tf.variable_scope('gcn_scope'):
            cur_node_states = self.placeholders['initial_node_representation']  # number of nodes in batch v x D
            num_nodes = tf.shape(self.placeholders['initial_node_representation'], out_type=tf.int64)[0]

            adjacency_matrix = tf.SparseTensor(indices=self.placeholders['adjacency_list'],
                            values=self.placeholders['adjacency_weights'],
                                               dense_shape=[num_nodes, num_nodes])

            for layer_idx in range(self.params['num_timesteps']):
                scaled_cur_node_states = tf.sparse_tensor_dense_matmul(adjacency_matrix, cur_node_states)  # v x D
                new_node_states = tf.matmul(scaled_cur_node_states, self.weights['edge_weights'][layer_idx])
                from IPython import embed; embed()
                ### Attention here
                '''
                    e_{ij} = a(hi, hj)
                    alpha_{ij} = softmax(e_{ij})
                    h_i = relu(sum alpha_ij * h_ij)

                    x = f(hh')*A
                    x = x / norm(x, 1)
                    x = x * h
                    tf.tensordot(A, B, axes=0)
                    x = tf.sparse.matmul(new_node_states, tf.sparse.transpose(new_node_states))
                    x = tf.sparse.matmul(x, adjacency_matrix)
                    norm_row = tf.norm(x, 2, axis=1)
                    x = x/norm_row
                    x = tf.sparse.matmul(x, new_node_states)
                    # x = tf.relu(x)
                '''



                if self.params['gcn_use_bias']:
                    new_node_states += self.weights['edge_biases'][layer_idx]  # v x D

                # On all but final layer do ReLU and dropout:
                if layer_idx < self.params['num_timesteps'] - 1:
                    new_node_states = tf.nn.relu(new_node_states)
                    new_node_states = tf.nn.dropout(new_node_states, keep_prob=self.placeholders['graph_state_keep_prob'])

                cur_node_states = new_node_states

            return cur_node_states

    ## DIFFERENCE:
    # last_h
    # regression_gate: function ? network for task i - is that edge type and f_k
    # regression_transform: fucntion ? network
    def gated_regression(self, last_h, regression_gate, regression_transform):
        # last_h: [v x h]
        gate_input = tf.concat([last_h, self.placeholders['initial_node_representation']], axis=-1)  # [v x 2h]
        gated_outputs = tf.nn.sigmoid(regression_gate(gate_input)) * regression_transform(last_h)    # [v x 1]

        # Sum up all nodes per-graph
        graph_representations = tf.unsorted_segment_sum(data=gated_outputs,
                                                        segment_ids=self.placeholders['graph_nodes_list'],
                                                        num_segments=self.placeholders['num_graphs'])  # [g x 1]
        self.output = tf.squeeze(graph_representations)
        return self.output # [g]

    # ----- Data preprocessing and chunking into minibatches:
    ## OVERWRITE
    def process_raw_graphs(self, raw_data: Sequence[Any], is_training_data: bool) -> Any:
        processed_graphs = []
        for d in raw_data:
            (adjacency_list, adjacency_weights) = self.__graph_to_adjacency_list(d['graph'], len(d["node_features"]))
            processed_graphs.append({"adjacency_list": adjacency_list,
                                     "adjacency_weights": adjacency_weights,
                                     "init": d["node_features"],
                                     "labels": [d["targets"][task_id][0] for task_id in self.params['task_ids']]})

        if is_training_data:
            np.random.shuffle(processed_graphs)
            for task_id in self.params['task_ids']:
                task_sample_ratio = self.params['task_sample_ratios'].get(str(task_id))
                if task_sample_ratio is not None:
                    ex_to_sample = int(len(processed_graphs) * task_sample_ratio)
                    for ex_id in range(ex_to_sample, len(processed_graphs)):
                        processed_graphs[ex_id]['labels'][task_id] = None

        return processed_graphs

    def process_raw_graphs_dense(self, raw_data: Sequence[Any], is_training_data: bool, bucket_sizes=None) -> Any:
        if bucket_sizes is None:
            bucket_sizes = np.array(list(range(4, 28, 2)) + [29])
        bucketed = defaultdict(list)
        x_dim = len(raw_data[0]["node_features"][0])
        for d in raw_data:
            chosen_bucket_idx = np.argmax(bucket_sizes > max([v for e in d['graph']
                                                                for v in [e[0], e[2]]]))
            chosen_bucket_size = bucket_sizes[chosen_bucket_idx]
            n_active_nodes = len(d["node_features"])
            bucketed[chosen_bucket_idx].append({
                'adj_mat': graph_to_adj_mat(d['graph'], chosen_bucket_size, self.num_edge_types, self.params['tie_fwd_bkwd']),
                'init': d["node_features"] + [[0 for _ in range(x_dim)] for __ in
                                              range(chosen_bucket_size - n_active_nodes)],
                'labels': [d["targets"][task_id][0] for task_id in self.params['task_ids']],
                'mask': [1. for _ in range(n_active_nodes) ] + [0. for _ in range(chosen_bucket_size - n_active_nodes)]
            })

        if is_training_data:
            for (bucket_idx, bucket) in bucketed.items():
                np.random.shuffle(bucket)
                for task_id in self.params['task_ids']:
                    task_sample_ratio = self.params['task_sample_ratios'].get(str(task_id))
                    if task_sample_ratio is not None:
                        ex_to_sample = int(len(bucket) * task_sample_ratio)
                        for ex_id in range(ex_to_sample, len(bucket)):
                            bucket[ex_id]['labels'][task_id] = None

        bucket_at_step = [[bucket_idx for _ in range(len(bucket_data) // self.params['batch_size'])]
                          for bucket_idx, bucket_data in bucketed.items()]
        bucket_at_step = [x for y in bucket_at_step for x in y]

        return (bucketed, bucket_sizes, bucket_at_step)

    def __graph_to_adjacency_list(self, graph, num_nodes: int) -> Tuple[np.ndarray, np.ndarray]:
        # Step 1: Generate adjacency matrices:
        adj_matrix = np.zeros((num_nodes, num_nodes))
        for src, _, dest in graph:
            adj_matrix[src, dest] = 1
            adj_matrix[dest, src] = 1

        # Step 2: Introduce self loops:
        self_loops = np.eye(num_nodes)
        adj_matrix += self_loops

        # Step 3: Normalize adj_matrices so that scale of vectors doesn't explode:
        row_sum = np.sum(adj_matrix, axis=-1)
        D_inv_sqrt = np.diag(np.power(row_sum, -0.5).flatten() + 1e-7)
        adj_matrix = D_inv_sqrt.dot(adj_matrix).dot(D_inv_sqrt)

        # Step 4: Turn into sorted adjacency lists:
        final_adj_list = []
        final_adj_weights = []
        for i in range(num_nodes):
            for j in range(num_nodes):
                w = adj_matrix[i, j]
                if w != 0:
                    final_adj_list.append([i,j])
                    final_adj_weights.append(w)

        return np.array(final_adj_list), np.array(final_adj_weights)

    def make_batch(self, elements):
        batch_data = {'adj_mat': [], 'init': [], 'labels': [], 'node_mask': [], 'task_masks': []}
        for d in elements:
            batch_data['adj_mat'].append(d['adj_mat'])
            batch_data['init'].append(d['init'])
            batch_data['node_mask'].append(d['mask'])

            target_task_values = []
            target_task_mask = []
            for target_val in d['labels']:
                if target_val is None:  # This is one of the examples we didn't sample...
                    target_task_values.append(0.)
                    target_task_mask.append(0.)
                else:
                    target_task_values.append(target_val)
                    target_task_mask.append(1.)
            batch_data['labels'].append(target_task_values)
            batch_data['task_masks'].append(target_task_mask)

        return batch_data

    ## OVERWRITE
    def make_minibatch_iterator(self, data: Any, is_training: bool):
        """Create minibatches by flattening adjacency matrices into a single adjacency matrix with
        multiple disconnected components."""
        if is_training:
            np.random.shuffle(data)
        dropout_keep_prob = self.params['graph_state_dropout_keep_prob'] if is_training else 1.
        # Pack until we cannot fit more graphs in the batch
        num_graphs = 0
        while num_graphs < len(data):
            num_graphs_in_batch = 0
            batch_node_features = []
            batch_target_task_values = []
            batch_target_task_mask = []
            batch_adjacency_list = []
            batch_adjacency_weights = []
            batch_graph_nodes_list = []
            node_offset = 0

            while num_graphs < len(data) and node_offset + len(data[num_graphs]['init']) < self.params['batch_size']:
                cur_graph = data[num_graphs]
                num_nodes_in_graph = len(cur_graph['init'])

                padded_features = np.pad(cur_graph['init'],
                                         ((0, 0), (0, self.params['hidden_size'] - self.annotation_size)),
                                         mode='constant')
                batch_node_features.extend(padded_features)

                batch_graph_nodes_list.append(np.full(shape=[num_nodes_in_graph], fill_value=num_graphs_in_batch, dtype=np.int32))

                batch_adjacency_list.append(cur_graph['adjacency_list'] + node_offset)

                batch_adjacency_weights.append(cur_graph['adjacency_weights'])

                target_task_values = []
                target_task_mask = []
                for target_val in cur_graph['labels']:
                    if target_val is None:  # This is one of the examples we didn't sample...
                        target_task_values.append(0.)
                        target_task_mask.append(0.)
                    else:
                        target_task_values.append(target_val)
                        target_task_mask.append(1.)
                batch_target_task_values.append(target_task_values)
                batch_target_task_mask.append(target_task_mask)
                num_graphs += 1
                num_graphs_in_batch += 1
                node_offset += num_nodes_in_graph

            batch_feed_dict = {
                self.placeholders['initial_node_representation']: np.array(batch_node_features),
                self.placeholders['adjacency_list']: np.concatenate(batch_adjacency_list, axis=0),
                self.placeholders['adjacency_weights']: np.concatenate(batch_adjacency_weights, axis=0),
                self.placeholders['graph_nodes_list']: np.concatenate(batch_graph_nodes_list, axis=0),
                self.placeholders['target_values']: np.transpose(batch_target_task_values, axes=[1,0]),
                self.placeholders['target_mask']: np.transpose(batch_target_task_mask, axes=[1, 0]),
                self.placeholders['num_graphs']: num_graphs_in_batch,
                self.placeholders['graph_state_keep_prob']: dropout_keep_prob,
            }

            yield batch_feed_dict

    def evaluate_one_batch(self, initial_node_representations, adjacency_matrices, node_masks=None):
        num_vertices = len(initial_node_representations[0])
        if node_masks is None:
            node_masks = []
            for r in initial_node_representations:
                node_masks.append([1. for _ in r] + [0. for _ in range(num_vertices - len(r))])

        batch_feed_dict = {
            self.placeholders['initial_node_representation']: np.array(initial_node_representations),
            self.placeholders['num_graphs']: len(initial_node_representations),
            self.placeholders['adjacency_list']: np.concatenate(adjacency_matrices, axis=0),
            self.placeholders['node_mask']: node_masks,
            self.placeholders['graph_state_keep_prob']: 1.0,
            self.placeholders['out_layer_dropout_keep_prob']: 1.0,
            self.placeholders['edge_weight_dropout_keep_prob']: 1.0
        }

        fetch_list = self.output
        result = self.sess.run(fetch_list, feed_dict=batch_feed_dict)

        return result

    def example_evaluation(self):
        ''' Demonstration of what test-time code would look like
        we query the model with the first n_example_molecules from the validation file
        '''
        n_example_molecules = 10
        with open('molecules_valid.json', 'r') as valid_file:
            example_molecules = json.load(valid_file)[:n_example_molecules]

        ### what is this?
        for mol in example_molecules:
            print(mol['targets'])

        # Process raw graph
        # Process raw graph
        example_molecules, _, _ = self.process_raw_graphs_dense(example_molecules,
            is_training_data=False, bucket_sizes=np.array([29]))

        batch_data = self.make_batch(example_molecules[0])

        print(self.evaluate_one_batch(batch_data['init'], batch_data['adj_mat']))


def main():
    args = docopt(__doc__)
    try:
        model = SparseGCNChemModel(args)
        if args['--evaluate']:
            model.example_evaluation()
        else:
            model.train()
    except:
        typ, value, tb = sys.exc_info()
        traceback.print_exc()
        pdb.post_mortem(tb)

if __name__ == "__main__":
    main()

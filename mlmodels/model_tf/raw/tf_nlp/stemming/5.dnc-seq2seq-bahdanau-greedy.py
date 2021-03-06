#!/usr/bin/env python
# coding: utf-8

# In[1]:


import collections
import itertools
import os
import re
import time

import numpy as np
import tensorflow as tf
from sklearn.utils import shuffle

from dnc import DNC

# In[2]:


def build_dataset(words, n_words, atleast=1):
    count = [["GO", 0], ["PAD", 1], ["EOS", 2], ["UNK", 3]]
    counter = collections.Counter(words).most_common(n_words)
    counter = [i for i in counter if i[1] >= atleast]
    count.extend(counter)
    dictionary = dict()
    for word, _ in count:
        dictionary[word] = len(dictionary)
    data = list()
    unk_count = 0
    for word in words:
        index = dictionary.get(word, 0)
        if index == 0:
            unk_count += 1
        data.append(index)
    count[0][1] = unk_count
    reversed_dictionary = dict(zip(dictionary.values(), dictionary.keys()))
    return data, count, dictionary, reversed_dictionary


# In[3]:


with open("lemmatization-en.txt", "r") as fopen:
    texts = fopen.read().split("\n")
after, before = [], []
for i in texts[:1000]:
    splitted = i.encode("ascii", "ignore").decode("utf-8").lower().split("\t")
    if len(splitted) < 2:
        continue
    after.append(list(splitted[0]))
    before.append(list(splitted[1]))

print(len(after), len(before))


# In[4]:


concat_from = list(itertools.chain(*before))
vocabulary_size_from = len(list(set(concat_from)))
data_from, count_from, dictionary_from, rev_dictionary_from = build_dataset(
    concat_from, vocabulary_size_from
)
print("vocab from size: %d" % (vocabulary_size_from))
print("Most common words", count_from[4:10])
print("Sample data", data_from[:10], [rev_dictionary_from[i] for i in data_from[:10]])
print("filtered vocab size:", len(dictionary_from))
print("% of vocab used: {}%".format(round(len(dictionary_from) / vocabulary_size_from, 4) * 100))


# In[5]:


concat_to = list(itertools.chain(*after))
vocabulary_size_to = len(list(set(concat_to)))
data_to, count_to, dictionary_to, rev_dictionary_to = build_dataset(concat_to, vocabulary_size_to)
print("vocab from size: %d" % (vocabulary_size_to))
print("Most common words", count_to[4:10])
print("Sample data", data_to[:10], [rev_dictionary_to[i] for i in data_to[:10]])
print("filtered vocab size:", len(dictionary_to))
print("% of vocab used: {}%".format(round(len(dictionary_to) / vocabulary_size_to, 4) * 100))


# In[6]:


GO = dictionary_from["GO"]
PAD = dictionary_from["PAD"]
EOS = dictionary_from["EOS"]
UNK = dictionary_from["UNK"]


# In[7]:


for i in range(len(after)):
    after[i].append("EOS")


# In[8]:


num_reads = 5
num_writes = 1
memory_size = 128
word_size = 128
clip_value = 20


# In[9]:


class Stemmer:
    def __init__(
        self,
        size_layer,
        num_layers,
        embedded_size,
        from_dict_size,
        to_dict_size,
        learning_rate,
        batch_size,
        attn_input_feeding=True,
    ):
        def attn_decoder_input_fn(inputs, attention):
            if attn_input_feeding:
                return inputs

        def attention(encoder_out, cell, seq_len, encoder_last_state, reuse=False):
            attention_mechanism = tf.contrib.seq2seq.BahdanauAttention(
                num_units=size_layer, memory=encoder_out, memory_sequence_length=seq_len
            )
            return tf.contrib.seq2seq.AttentionWrapper(
                cell=cell,
                attention_mechanism=attention_mechanism,
                attention_layer_size=size_layer,
                cell_input_fn=attn_decoder_input_fn,
                initial_cell_state=encoder_last_state,
                alignment_history=False,
            )

        self.X = tf.placeholder(tf.int32, [None, None])
        self.Y = tf.placeholder(tf.int32, [None, None])
        self.X_seq_len = tf.placeholder(tf.int32, [None])
        self.Y_seq_len = tf.placeholder(tf.int32, [None])
        access_config = {
            "memory_size": memory_size,
            "word_size": word_size,
            "num_reads": num_reads,
            "num_writes": num_writes,
        }
        controller_config = {"hidden_size": size_layer}
        self.dnc_cell = DNC(
            access_config=access_config,
            controller_config=controller_config,
            output_size=size_layer,
            clip_value=clip_value,
        )
        self.dnc_initial = self.dnc_cell.initial_state

        encoder_embeddings = tf.Variable(tf.random_uniform([from_dict_size, embedded_size], -1, 1))
        encoder_embedded = tf.nn.embedding_lookup(encoder_embeddings, self.X)

        initial_state = self.dnc_initial(batch_size)
        self.encoder_out, self.encoder_state = tf.nn.dynamic_rnn(
            cell=self.dnc_cell,
            inputs=encoder_embedded,
            sequence_length=self.X_seq_len,
            dtype=tf.float32,
            initial_state=initial_state,
        )
        main = tf.strided_slice(self.Y, [0, 0], [batch_size, -1], [1, 1])
        decoder_input = tf.concat([tf.fill([batch_size, 1], GO), main], 1)
        # decoder
        decoder_embeddings = tf.Variable(tf.random_uniform([to_dict_size, embedded_size], -1, 1))
        decoder_cell = attention(
            self.encoder_out, self.dnc_cell, self.X_seq_len, self.encoder_state
        )
        dense_layer = tf.layers.Dense(to_dict_size)
        training_helper = tf.contrib.seq2seq.TrainingHelper(
            inputs=tf.nn.embedding_lookup(decoder_embeddings, decoder_input),
            sequence_length=self.Y_seq_len,
            time_major=False,
        )
        training_decoder = tf.contrib.seq2seq.BasicDecoder(
            cell=decoder_cell,
            helper=training_helper,
            initial_state=decoder_cell.zero_state(batch_size=batch_size, dtype=tf.float32),
            output_layer=dense_layer,
        )
        training_decoder_output, _, _ = tf.contrib.seq2seq.dynamic_decode(
            decoder=training_decoder,
            impute_finished=True,
            output_time_major=False,
            maximum_iterations=tf.reduce_max(self.Y_seq_len),
        )

        predicting_helper = tf.contrib.seq2seq.GreedyEmbeddingHelper(
            embedding=decoder_embeddings,
            start_tokens=tf.tile(tf.constant([GO], dtype=tf.int32), [batch_size]),
            end_token=EOS,
        )
        predicting_decoder = tf.contrib.seq2seq.BasicDecoder(
            cell=decoder_cell,
            helper=predicting_helper,
            initial_state=decoder_cell.zero_state(batch_size=batch_size, dtype=tf.float32),
            output_layer=dense_layer,
        )
        predicting_decoder_output, _, _ = tf.contrib.seq2seq.dynamic_decode(
            decoder=predicting_decoder,
            impute_finished=True,
            maximum_iterations=2 * tf.reduce_max(self.X_seq_len),
        )
        self.training_logits = training_decoder_output.rnn_output
        self.predicting_ids = predicting_decoder_output.sample_id
        masks = tf.sequence_mask(self.Y_seq_len, tf.reduce_max(self.Y_seq_len), dtype=tf.float32)
        self.cost = tf.contrib.seq2seq.sequence_loss(
            logits=self.training_logits, targets=self.Y, weights=masks
        )
        self.optimizer = tf.train.AdamOptimizer(learning_rate).minimize(self.cost)


# In[10]:


size_layer = 128
num_layers = 2
embedded_size = 32
learning_rate = 1e-3
batch_size = 32
epoch = 50


# In[11]:


tf.reset_default_graph()
sess = tf.InteractiveSession()
model = Stemmer(
    size_layer,
    num_layers,
    embedded_size,
    len(dictionary_from),
    len(dictionary_to),
    learning_rate,
    batch_size,
)
sess.run(tf.global_variables_initializer())


# In[12]:


def str_idx(corpus, dic):
    X = []
    for i in corpus:
        ints = []
        for k in i:
            try:
                ints.append(dic[k])
            except Exception as e:
                ints.append(UNK)
        X.append(ints)
    return X


# In[13]:


X = str_idx(before, dictionary_from)
Y = str_idx(after, dictionary_to)


# In[14]:


def pad_sentence_batch(sentence_batch, pad_int):
    padded_seqs = []
    seq_lens = []
    max_sentence_len = max([len(sentence) for sentence in sentence_batch])
    for sentence in sentence_batch:
        padded_seqs.append(sentence + [pad_int] * (max_sentence_len - len(sentence)))
        seq_lens.append(len(sentence))
    return padded_seqs, seq_lens


def check_accuracy(logits, Y):
    acc = 0
    for i in range(logits.shape[0]):
        internal_acc = 0
        count = 0
        for k in range(len(Y[i])):
            try:
                if Y[i][k] == logits[i][k]:
                    internal_acc += 1
                count += 1
                if Y[i][k] == EOS:
                    break
            except:
                break
        acc += internal_acc / count
    return acc / logits.shape[0]


# In[15]:


for i in range(epoch):
    total_loss, total_accuracy = 0, 0
    X, Y = shuffle(X, Y)
    for k in range(0, (len(before) // batch_size) * batch_size, batch_size):
        batch_x, seq_x = pad_sentence_batch(X[k : k + batch_size], PAD)
        batch_y, seq_y = pad_sentence_batch(Y[k : k + batch_size], PAD)
        predicted, loss, _ = sess.run(
            [model.predicting_ids, model.cost, model.optimizer],
            feed_dict={
                model.X: batch_x,
                model.Y: batch_y,
                model.X_seq_len: seq_x,
                model.Y_seq_len: seq_y,
            },
        )
        total_loss += loss
        total_accuracy += check_accuracy(predicted, batch_y)
    total_loss /= len(before) // batch_size
    total_accuracy /= len(before) // batch_size
    print("epoch: %d, avg loss: %f, avg accuracy: %f" % (i + 1, total_loss, total_accuracy))


# In[16]:


for i in range(len(batch_x)):
    print("row %d" % (i + 1))
    print("BEFORE:", "".join([rev_dictionary_from[n] for n in batch_x[i] if n not in [0, 1, 2, 3]]))
    print(
        "REAL AFTER:", "".join([rev_dictionary_to[n] for n in batch_y[i] if n not in [0, 1, 2, 3]])
    )
    print(
        "PREDICTED AFTER:",
        "".join([rev_dictionary_to[n] for n in predicted[i] if n not in [0, 1, 2, 3]]),
        "\n",
    )


# In[ ]:

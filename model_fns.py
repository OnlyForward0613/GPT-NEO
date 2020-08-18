import mesh_tensorflow as mtf
import tensorflow.compat.v1 as tf
from tensorflow.python.tpu import tpu_estimator
import mesh_tensorflow.auto_mtf
import mesh_tensorflow.transformer as mtf_transformer
from collections import defaultdict

from optimizers import get_optimizer
from utils import (TpuSummaries, get_graph_info)
from models.utils import biasmask_attn_weights
from tensorflow.python.ops import resources
from sample import sample_autoregressive

from models.gpt2 import gpt2

def model_fn(features, labels, mode, params):
    # grab global step no.
    params = defaultdict(lambda: None, params)

    global_step = tf.train.get_global_step()

    # construct mtf graph + mesh from params
    graph = mtf.Graph()
    mesh_shape = mtf.convert_to_shape(params["mesh_shape"])
    layout_rules = mtf.convert_to_layout_rules(params["layout"])

    # init summary class
    summary = TpuSummaries(params["model_path"])

    # Mesh stuff
    if params["use_tpu"]:
        # construct SimdMesh function - instructions on how to evenly split tensors across all cores
        num_hosts = params['context'].num_hosts
        host_placement_fn = params['context'].tpu_host_placement_function
        device_list = [host_placement_fn(host_id=i) for i in range(num_hosts)]
        tf.logging.info('device_list = {}'.format(device_list))

        # TODO: Better estimation of replica cache size?
        replica_cache_size = 300 * 1000000  # 300M per replica

        # Worker 0 caches all the TPU binaries.
        worker0_mem = replica_cache_size * params['context'].num_replicas
        devices_memory_usage = [worker0_mem] + [0] * (num_hosts - 1)
        var_placer = mtf.utils.BalancedVariablePlacer(device_list, devices_memory_usage)
        mesh_devices = [''] * mesh_shape.size
        mesh_impl = mtf.simd_mesh_impl.SimdMeshImpl(
            mesh_shape, layout_rules, mesh_devices, params['context'].device_assignment)
    else:
        var_placer = None

        mesh_devices = ['/device:GPU:0']
        mesh_shape = [("all_processors", 1)]
        layout_rules = [("batch", "all_processors")]

        mesh_impl = mtf.placement_mesh_impl.PlacementMeshImpl(
            mesh_shape, layout_rules, mesh_devices)

    # Build the actual model
    mesh = mtf.Mesh(graph, 'my_mesh', var_placer)

    # build mtf_features & seq length dict for getting number of microbatches
    # we need to pack inputs into a dict to pass into serialize_training_step
    features_dict = {"inputs": features, "labels": labels}
    sequence_length_dict = {"inputs": params["n_ctx"], "labels": params["n_ctx"]}

    batch_size = 1 if mode == tf.estimator.ModeKeys.PREDICT else params["train_batch_size"]
    batch_dim = mtf.Dimension('batch', batch_size)
    batch_dims = [batch_dim]
    feature_length = sequence_length_dict["inputs"]
    length_dim = mtf.Dimension("sequence", feature_length)

    mtf_features = {}
    for key, x in features_dict.items():
        if x is not None:
            feature_shape = mtf.Shape(batch_dims + [length_dim])
            x = tf.cast(features_dict[key], tf.int32)
            x = tf.reshape(x, feature_shape.to_integer_list)
            mtf_features[key] = mtf.import_fully_replicated(
                mesh, x, feature_shape, name=key)

    # instantiate dict for dimensions, bias, etc that can be calculated here once then passed into model
    other_features = {}
    # Calculate attn mask (attn_bias) once here
    memory_length_dim = mtf.Dimension("memory_length", length_dim.size)
    attn_bias = biasmask_attn_weights(mesh, length_dim, memory_length_dim, tf.float32)

    # add attn_bias into mtf_features
    other_features["attn_bias"] = attn_bias

    # define other Dimensions that we'll need inside the model
    embd_dim = mtf.Dimension("embd", params["n_embd"])
    vocab_dim = mtf.Dimension("vocab", params["n_vocab"])
    # we need this because gathering when both the args have the same dimension in them breaks things
    # this dim is specifically for the weights
    # this prevents the "Einsum has lhs dimension without corresponding rhs or output dimension." error.
    embed_sequence_dim = mtf.Dimension('embed_sequence', params["n_ctx"]) # TODO: this could be memory_length?

    other_features["embd_dim"] = embd_dim
    other_features["vocab_dim"] = vocab_dim
    other_features["embed_sequence_dim"] = embed_sequence_dim
    other_features["memory_length_dim"] = memory_length_dim

    if mode == tf.estimator.ModeKeys.PREDICT:
        inputs = mtf_features["inputs"]
        mtf_samples = sample_autoregressive(
            inputs, other_features=other_features, params=params, variable_dtype=tf.float32,
            remove_partial_sequences=True)
        mtf_samples = mtf.anonymize(mtf_samples)
        inputs = mtf.anonymize(inputs)
        lowering = mtf.Lowering(graph, {mesh: mesh_impl}, autostack=True)
        orig_inputs = lowering.export_to_tf_tensor(mtf_features["inputs"])
        inputs = mtf.transformer.utils.clean_decodes(lowering.export_to_tf_tensor(inputs))
        outputs = mtf.transformer.utils.clean_decodes(lowering.export_to_tf_tensor(mtf_samples))

        predictions = {
            "orig_inputs": orig_inputs,
            "inputs": inputs,
            "outputs": outputs}

        def scaffold_fn():
            return tf.train.Scaffold(
                local_init_op=tf.group(
                    tf.train.Scaffold.default_local_init_op(),
                    lowering.copy_masters_to_slices(),
                    name="mtf_local_init_op"),
                ready_op=tf.concat(
                    [tf.report_uninitialized_variables(),
                    resources.report_uninitialized_resources()],
                    axis=0,
                    name="mtf_ready_op"))

        return tpu_estimator.TPUEstimatorSpec(
            mode=tf.estimator.ModeKeys.PREDICT,
            predictions=predictions,
            scaffold_fn=scaffold_fn,
            prediction_hooks=[mtf.MtfRestoreHook(lowering)])


    # gets number of microbatches per batch for serialized training
    # if param tokens_per_mb_per_replica = None, this defaults to 1 and no microbatching is performed
    num_microbatches = int(mtf_transformer.utils.serialize_num_microbatches(batch_dim=batch_dim,
                                                                        sequence_length=sequence_length_dict,
                                                                        mesh_shape=mesh_shape,
                                                                        layout_rules=layout_rules,
                                                                        tokens_per_microbatch_per_replica=params["tokens_per_mb_per_replica"]))
    params["num_microbatches"] = num_microbatches  # add num microbatches to params

    if num_microbatches > 1:
        def serialized_fn(mtf_features):
            # for serialize_training_step we need to modify the model to output results in a dict
            if params["model"] == "GPT2":
                with tf.variable_scope('gpt2'):
                    logits, loss, loss_batch = gpt2.model(mtf_features, other_features, params, mesh)
                return {"logits": logits, "loss": loss, "loss_batch": loss_batch}

        # serialize the training step - Gradients are accumulated locally and reduced once.
        var_grads, output_dict = mtf.serialize_training_step(
            mtf_features, serialized_fn, batch_dim, num_microbatches)
        loss = output_dict["loss"]
        loss_batch = output_dict["loss_batch"]
        logits = output_dict["logits"]
    else:
        # if we're not splitting into microbatches, return logits & loss as is
        if params["model"] == "GPT2":
            with mtf.utils.outside_all_rewrites():
                with tf.variable_scope('gpt2'):
                    logits, loss, loss_batch = gpt2.model(mtf_features, other_features, params, mesh)
        else:
            raise Exception("{} is not a valid model - please select from GPT2".format(params['model']))

    # Auto layout generation
    # TODO: move to utils
    if params["auto_layout"]:
        layout_rules = mtf.auto_mtf.layout(graph, mesh_shape, [logits, loss])
        print('Auto-selected layout:')
        print(layout_rules)
        print('Re-initialize graph with selected layout')
        quit()  # TODO: It should be easy to just reinitialize everything with selected layout
    if params["auto_layout_and_mesh_shape"]:
        layout_rules, mesh_shape = mtf.auto_mtf.layout_and_mesh_shape(graph, params["num_cores"],
                                                                      [logits, loss], max_mesh_shape_dimensions=4)
        print('Num cores:')
        print(params["num_cores"])
        print('Auto-selected layout:')
        print(layout_rules)
        print('Auto-selected mesh shape:')
        print(mesh_shape)
        print('Re-initialize graph with selected layout & mesh shape')
        quit()  # TODO: It should be easy to just reinitialize everything with selected layout

    # TRAIN mode
    if mode == tf.estimator.ModeKeys.TRAIN:
        if params["num_microbatches"] > 1:
            # if we are splitting the batch into microbatches, var grads are created in the serialize_training_step fn
            # so we pass them in here
            lr, update_ops = get_optimizer(loss, params, summary, inp_var_grads=var_grads)
        else:
            # otherwise, they are created in the get_optimizer fn, so we leave inp_var_grads blank
            lr, update_ops = get_optimizer(loss, params, summary)
    else:
        # For now, we can only export fully-replicated tensors.
        # This has to be done before lowering or they will not be included in the graph
        fully_replicated_logits = mtf.anonymize(logits)
        fully_replicated_loss_batch = mtf.anonymize(loss_batch)

    # Gets info about no. trainable vars in the model & dimension names
    get_graph_info(graph)

    # 'lowers' mtf tensors into a tf graph - this enables us to export results as tf tensors
    lowering = mtf.Lowering(graph, {mesh: mesh_impl}, autostack=params["autostack"])
    tf_loss = tf.to_float(lowering.export_to_tf_tensor(loss))
    tf_loss_batch_log = tf.to_float(lowering.export_to_tf_tensor(loss_batch))

    if mode == tf.estimator.ModeKeys.TRAIN:
        # creates update ops to pass into optimizer
        tf_update_ops = [lowering.lowered_operation(op) for op in update_ops]
        tf_update_ops.append(tf.assign_add(global_step, 1))  # Need to manually increment global_step
        tf.logging.info('tf_update_ops: {}'.format(tf_update_ops))
        train_op = tf.group(tf_update_ops)
    else:
        tf_logits = lowering.export_to_tf_tensor(fully_replicated_logits)
        tf_loss_batch = tf.to_float(lowering.export_to_tf_tensor(fully_replicated_loss_batch))

    with mtf.utils.outside_all_rewrites():
        # Copy master variables to slices. Must be called first.
        restore_hook = mtf.MtfRestoreHook(lowering)
        if mode == tf.estimator.ModeKeys.TRAIN:
            saver = tf.train.Saver(
                tf.global_variables(),
                sharded=True,
                max_to_keep=10,
                keep_checkpoint_every_n_hours=2,
                defer_build=False,
                save_relative_paths=True)
            tf.add_to_collection(tf.GraphKeys.SAVERS, saver)
            saver_listener = mtf.MtfCheckpointSaverListener(lowering)
            saver_hook = tf.train.CheckpointSaverHook(
                params["model_path"],
                save_steps=params["steps_per_checkpoint"],
                saver=saver,
                listeners=[saver_listener])

            prepend_str = lambda text, sep, repeat: (sep * repeat) + " " + text

            log_data = {
                prepend_str('loss', '-', 40): tf_loss,
                prepend_str('batch loss', '-', 40): tf_loss_batch_log
            }

            logging_hook = tf.train.LoggingTensorHook(log_data, every_n_iter=10)

            return tpu_estimator.TPUEstimatorSpec(
                tf.estimator.ModeKeys.TRAIN,
                loss=tf_loss,
                host_call=summary.get_host_call(),
                train_op=train_op,
                training_hooks=[restore_hook, saver_hook, logging_hook])

        elif mode == tf.estimator.ModeKeys.EVAL:
            # evaluation metrics

            def _perplexity(tf_loss_batch):
                loss = tf.reduce_mean(tf_loss_batch)
                loss /= params["num_microbatches"]
                perplexity = tf.exp(loss)
                return tf.metrics.mean(perplexity)

            def _metric_fn(tf_logits, tf_loss_batch):
                mean_logits = tf.metrics.mean(tf_logits)
                perp = _perplexity(tf_loss_batch)
                return {'mean_logits': mean_logits, 'perplexity': perp}

            eval_metrics = (_metric_fn, [tf_logits, tf_loss_batch])

            return tpu_estimator.TPUEstimatorSpec(
                tf.estimator.ModeKeys.EVAL,
                evaluation_hooks=[restore_hook],
                loss=tf_loss,
                eval_metrics=eval_metrics)


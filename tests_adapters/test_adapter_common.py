import copy
import os
import tempfile

import torch

from transformers import (
    ADAPTER_CONFIG_MAP,
    ADAPTER_MODEL_MAPPING,
    MODEL_FOR_SEQ_TO_SEQ_CAUSAL_LM_MAPPING,
    AdapterSetup,
    AutoAdapterModel,
    HoulsbyConfig,
    HoulsbyInvConfig,
    MAMConfig,
    PfeifferConfig,
    PfeifferInvConfig,
    PrefixTuningConfig,
)
from transformers.adapters.utils import WEIGHTS_NAME
from transformers.testing_utils import require_torch, torch_device


def create_twin_models(model_class, config_creator=None):
    if config_creator and model_class.__name__.startswith("Auto"):
        model_config = config_creator()
        model1 = model_class.from_config(model_config)
    elif config_creator:
        model_config = config_creator()
        model1 = model_class(model_config)
    else:
        model_config = model_class.config_class()
        model1 = model_class(model_config)
    model1.eval()
    # create a twin initialized with the same random weights
    model2 = copy.deepcopy(model1)
    model2.eval()
    return model1, model2


@require_torch
class AdapterModelTestMixin:

    adapter_configs_to_test = [
        PfeifferConfig(),
        HoulsbyConfig(),
        PrefixTuningConfig(flat=True),
        MAMConfig(),
    ]

    def test_add_adapter(self):
        model = self.get_model()
        model.eval()

        for adapter_config in self.adapter_configs_to_test:
            with self.subTest(model_class=model.__class__.__name__, config=adapter_config.__class__.__name__):
                name = adapter_config.__class__.__name__
                model.add_adapter(name, config=adapter_config)
                model.set_active_adapters([name])

                # adapter is correctly added to config
                self.assertTrue(name in model.config.adapters)
                self.assertEqual(adapter_config, model.config.adapters.get(name))

                # check forward pass
                input_data = self.get_input_samples((1, 128), config=model.config)
                model.to(torch_device)
                adapter_output = model(**input_data)
                model.set_active_adapters(None)
                base_output = model(**input_data)
                self.assertEqual(len(adapter_output), len(base_output))
                self.assertFalse(torch.equal(adapter_output[0], base_output[0]))

    def test_delete_adapter(self):
        model = self.get_model()
        model.eval()

        for adapter_config in self.adapter_configs_to_test:
            with self.subTest(model_class=model.__class__.__name__, config=adapter_config.__class__.__name__):
                name = "test_adapter_" + adapter_config.__class__.__name__
                model.add_adapter(name, config="houlsby")
                model.set_active_adapters([name])

                # adapter is correctly added to config
                self.assertTrue(name in model.config.adapters)
                self.assertGreater(len(model.get_adapter(name)), 0)

                # remove the adapter again
                model.delete_adapter(name)
                self.assertFalse(name in model.config.adapters)
                self.assertEqual(len(model.get_adapter(name)), 0)

    def test_add_adapter_with_invertible(self):
        model = self.get_model()
        model.eval()

        for adapter_config in [PfeifferInvConfig(), HoulsbyInvConfig()]:
            with self.subTest(model_class=model.__class__.__name__, config=adapter_config.__class__.__name__):
                name = adapter_config.__class__.__name__
                model.add_adapter(name, config=adapter_config)
                model.set_active_adapters([name])

                # adapter is correctly added to config
                self.assertTrue(name in model.config.adapters)
                self.assertEqual(adapter_config, model.config.adapters.get(name))

                # invertible adapter is correctly added and returned
                self.assertTrue(name in model.invertible_adapters)
                self.assertEqual(model.invertible_adapters[name], model.get_invertible_adapter())

                # all invertible adapter weights should be activated for training
                for param in model.invertible_adapters[name].parameters():
                    self.assertTrue(param.requires_grad)

                # check forward pass
                input_data = self.get_input_samples((1, 128), config=model.config)
                model.to(torch_device)
                adapter_output = model(**input_data)
                # make sure the output is different without invertible adapter
                del model.invertible_adapters[name]
                adapter_output_no_inv = model(**input_data)
                self.assertEqual(len(adapter_output), len(adapter_output_no_inv))
                self.assertFalse(torch.equal(adapter_output[0], adapter_output_no_inv[0]))

    def test_get_adapter(self):
        model = self.get_model()
        model.eval()

        for adapter_config in self.adapter_configs_to_test:
            with self.subTest(model_class=model.__class__.__name__, config=adapter_config.__class__.__name__):
                model.add_adapter("first", config=adapter_config)
                model.add_adapter("second", config=adapter_config)
                model.set_active_adapters(["first"])

                # adapter is correctly added to config
                name = "first"
                self.assertTrue(name in model.config.adapters)
                self.assertEqual(adapter_config, model.config.adapters.get(name))

                first_adapter = model.get_adapter("first")
                second_adapter = model.get_adapter("second")

                self.assertNotEqual(len(first_adapter), 0)
                self.assertEqual(len(first_adapter), len(second_adapter))
                self.assertNotEqual(first_adapter, second_adapter)

                model.delete_adapter("first")
                model.delete_adapter("second")

    def test_add_adapter_multiple_reduction_factors(self):
        model = self.get_model()
        model.eval()
        reduction_factor = {"1": 1, "default": 2}
        for adapter_config in [
            PfeifferConfig(reduction_factor=reduction_factor),
            HoulsbyConfig(reduction_factor=reduction_factor),
        ]:
            with self.subTest(model_class=model.__class__.__name__, config=adapter_config.__class__.__name__):
                name = adapter_config.__class__.__name__
                model.add_adapter(name, config=adapter_config)
                model.set_active_adapters([name])

                # adapter is correctly added to config
                self.assertTrue(name in model.config.adapters)
                self.assertEqual(adapter_config, model.config.adapters.get(name))

                adapter = model.get_adapter(name)

                self.assertEqual(
                    adapter[0]["output_adapter"].adapter_down[0].in_features
                    / adapter[0]["output_adapter"].adapter_down[0].out_features,
                    reduction_factor["default"],
                )
                self.assertEqual(
                    adapter[1]["output_adapter"].adapter_down[0].in_features
                    / adapter[1]["output_adapter"].adapter_down[0].out_features,
                    reduction_factor["1"],
                )

    def test_reduction_factor_no_default(self):
        model = self.get_model()
        model.eval()
        reduction_factor = {"2": 8, "4": 32}
        for adapter_config in [
            PfeifferConfig(reduction_factor=reduction_factor),
            HoulsbyConfig(reduction_factor=reduction_factor),
        ]:
            with self.subTest(model_class=model.__class__.__name__, config=adapter_config.__class__.__name__):
                name = adapter_config.__class__.__name__
                with self.assertRaises(KeyError):
                    model.add_adapter(name, config=adapter_config)

    def test_adapter_forward(self):
        model = self.get_model()
        model.eval()

        for adapter_config in self.adapter_configs_to_test:
            with self.subTest(model_class=model.__class__.__name__, config=adapter_config.__class__.__name__):
                name = adapter_config.__class__.__name__
                model.add_adapter(name, config=adapter_config)
                model.to(torch_device)

                input_data = self.get_input_samples((1, 128), config=model.config)

                # set via property
                model.set_active_adapters([name])
                output_1 = model(**input_data)

                # unset and make sure it's unset
                model.set_active_adapters(None)
                self.assertEqual(None, model.active_adapters)

                # check forward pass
                with AdapterSetup(name):
                    output_2 = model(**input_data)
                self.assertEqual(len(output_1), len(output_2))
                self.assertTrue(torch.equal(output_1[0], output_2[0]))

    def run_load_test(self, config):
        model1, model2 = create_twin_models(self.model_class, self.config)

        name = "dummy_adapter"
        model1.add_adapter(name, config=config)
        model1.set_active_adapters([name])
        with tempfile.TemporaryDirectory() as temp_dir:
            model1.save_adapter(temp_dir, name)

            # Check that there are actually weights saved
            weights = torch.load(os.path.join(temp_dir, WEIGHTS_NAME), map_location="cpu")
            self.assertTrue(len(weights) > 0)

            # also tests that set_active works
            model2.load_adapter(temp_dir, set_active=True)

        # check if adapter was correctly loaded
        self.assertTrue(name in model2.config.adapters)

        # check equal output
        input_data = self.get_input_samples((1, 128), config=model1.config)
        model1.to(torch_device)
        model2.to(torch_device)
        output1 = model1(**input_data)
        output2 = model2(**input_data)
        self.assertEqual(len(output1), len(output2))
        self.assertTrue(torch.equal(output1[0], output2[0]))

    def test_load_adapter(self):
        self.run_load_test(PfeifferConfig())

    def test_load_prefix_tuning(self):
        self.run_load_test(PrefixTuningConfig())

    def test_load_mam_adapter(self):
        self.run_load_test(MAMConfig())

    def test_load_full_model(self):
        model1 = self.get_model()
        model1.eval()

        name = "dummy"
        model1.add_adapter(name)
        model1.set_active_adapters([name])
        with tempfile.TemporaryDirectory() as temp_dir:
            model1.save_pretrained(temp_dir)

            model2 = self.model_class.from_pretrained(temp_dir)
            model2.set_active_adapters([name])

        # check if adapter was correctly loaded
        self.assertTrue(name in model2.config.adapters)

        # check equal output
        input_data = self.get_input_samples((1, 128), config=model1.config)
        model1.to(torch_device)
        model2.to(torch_device)
        output1 = model1(**input_data)
        output2 = model2(**input_data)
        self.assertEqual(len(output1), len(output2))
        self.assertTrue(torch.equal(output1[0], output2[0]))

    def test_model_config_serialization(self):
        """PretrainedConfigurations should not raise an Exception when serializing the config dict

        See, e.g., PretrainedConfig.to_json_string()
        """
        for k, v in ADAPTER_CONFIG_MAP.items():
            model = self.get_model()
            # HACK: reduce the reduction factor such that
            # the small test model can have a phm_dim of 4
            if hasattr(v, "phm_layer") and v.phm_layer:
                v = v.__class__(reduction_factor=4)
            model.add_adapter("test", config=v)
            # should not raise an exception
            model.config.to_json_string()

    def test_loading_adapter_weights_with_prefix(self):
        if self.config_class not in ADAPTER_MODEL_MAPPING:
            self.skipTest("Does not support flex heads.")

        model_base, model_with_head_base = create_twin_models(self.model_class, self.config)

        model_with_head = AutoAdapterModel.from_config(model_with_head_base.config)
        setattr(model_with_head, model_with_head.base_model_prefix, model_with_head_base)

        model_with_head.add_adapter("dummy")

        with tempfile.TemporaryDirectory() as temp_dir:
            model_with_head.save_adapter(temp_dir, "dummy")

            loading_info = {}
            model_base.load_adapter(temp_dir, loading_info=loading_info)

        self.assertEqual(0, len(loading_info["missing_keys"]))
        self.assertEqual(0, len(loading_info["unexpected_keys"]))

        # check equal output
        input_data = self.get_input_samples((1, 128), config=model_with_head.config)
        model_with_head.to(torch_device)
        model_base.to(torch_device)
        output1 = model_with_head(**input_data)
        output2 = model_base(**input_data)
        self.assertEqual(len(output1), len(output2))
        self.assertTrue(torch.equal(output1[0], output2[0]))

    def test_loading_adapter_weights_without_prefix(self):
        if self.config_class not in ADAPTER_MODEL_MAPPING:
            self.skipTest("Does not support flex heads.")

        model_base, model_with_head_base = create_twin_models(self.model_class, self.config)

        model_with_head = AutoAdapterModel.from_config(model_with_head_base.config)
        setattr(model_with_head, model_with_head.base_model_prefix, model_with_head_base)

        model_base.add_adapter("dummy")

        with tempfile.TemporaryDirectory() as temp_dir:
            model_base.save_adapter(temp_dir, "dummy")

            loading_info = {}
            model_with_head.load_adapter(temp_dir, loading_info=loading_info)

        self.assertEqual(0, len(loading_info["missing_keys"]))
        self.assertEqual(0, len(loading_info["unexpected_keys"]))

        # check equal output
        input_data = self.get_input_samples((1, 128), config=model_with_head.config)
        model_with_head.to(torch_device)
        model_base.to(torch_device)
        output1 = model_with_head(**input_data)
        output2 = model_base(**input_data)
        self.assertEqual(len(output1), len(output2))
        self.assertTrue(torch.equal(output1[0], output2[0]))

    def test_forward_with_past(self):
        if self.config_class not in ADAPTER_MODEL_MAPPING:
            self.skipTest("Does not support flex heads.")
        if self.config_class not in MODEL_FOR_SEQ_TO_SEQ_CAUSAL_LM_MAPPING:
            self.skipTest("No causal lm class.")

        static_model = MODEL_FOR_SEQ_TO_SEQ_CAUSAL_LM_MAPPING[self.config_class](self.config())
        flex_model = AutoAdapterModel.from_pretrained(
            None, config=self.config(), state_dict=static_model.state_dict()
        )
        static_model.add_adapter("dummy")
        static_model.set_active_adapters("dummy")
        static_model.eval()
        flex_model.eval()

        with tempfile.TemporaryDirectory() as temp_dir:
            static_model.save_adapter(temp_dir, "dummy")

            loading_info = {}
            flex_model.load_adapter(temp_dir, loading_info=loading_info)
            flex_model.set_active_adapters("dummy")

        input_data = self.get_input_samples((1, 128), config=static_model.config)
        static_model.eval()
        flex_model.eval()
        static_model.to(torch_device)
        flex_model.to(torch_device)
        output = static_model(**input_data)

        input_data["past_key_values"] = output["past_key_values"]
        output_base = static_model(**input_data)
        output_with_head = flex_model(**input_data)
        self.assertTrue(torch.allclose(output_base["logits"], output_with_head["logits"]))

    def test_eject_prefix(self):
        model = self.get_model()
        model.eval()
        model.add_adapter("test_prefix", config="prefix_tuning")
        model.to(torch_device)

        input_data = self.get_input_samples((2, 128), config=model.config)

        # user reparamterized prefix
        model.set_active_adapters(["test_prefix"])
        output_1 = model(**input_data)

        # eject prefix
        model.eject_prefix_tuning("test_prefix")
        model.to(torch_device)
        model.eval()
        output_2 = model(**input_data)

        # check forward pass
        self.assertEqual(len(output_1), len(output_2))
        self.assertTrue(torch.allclose(output_1[0], output_2[0], atol=1e-4))

    def test_save_all_adapters_with_head(self):
        if self.config_class not in ADAPTER_MODEL_MAPPING:
            self.skipTest("Does not support flex heads.")

        model = AutoAdapterModel.from_config(self.config())
        model.eval()
        model.add_adapter("test")
        self.add_head(model, "test")
        with tempfile.TemporaryDirectory() as tmp_dir:
            model.save_all_adapters(tmp_dir, with_head=True)
            self.assertTrue(os.path.isfile(os.path.join(tmp_dir, "test", "head_config.json")))

        with tempfile.TemporaryDirectory() as tmp_dir:
            model.save_all_adapters(tmp_dir, with_head=False)
            self.assertFalse(os.path.isfile(os.path.join(tmp_dir, "test", "head_config.json")))


import functools
import importlib
import inspect
import io
import logging
import os
import random
import sys
import typing
from enum import Enum
from pathlib import Path
from typing import Callable, Union, get_args, get_origin

from colorama import Fore

import numpy as np
import torch
from PIL import Image

import nodes


# Export the web directory so ComfyUI can pick up the JavaScript.
web_path = os.path.join(os.path.dirname(__file__), "web")
if os.path.exists(web_path):
    nodes.EXTENSION_WEB_DIRS["ComfyUI-Annotations"] = os.path.join(os.path.dirname(__file__), "web")
    logging.info(f"Registered ComfyUI-Annotations web directory: '{web_path}'")
else:
    logging.warning(f"ComfyUI-Annotations: Web directory not found at {web_path}. Some features may not be available.")


default_category = "ComfyFunc"

# Whether or not to reload modules before running the nodes.
# Useful while developing.
reload_modules = True

class AutoDescriptionMode(Enum):
    NONE = "none"
    BRIEF = "brief"
    FULL = "full"

# Whether to automatically use the docstring as the description for nodes.
# If set to AutoDescriptionMode.FULL, the full docstring will be used, whereas
# AutoDescriptionMode.BRIEF will use only the first line of the docstring.
docstring_mode = AutoDescriptionMode.FULL

# Whether to automatically verify tensor shapes on input and output for common tensor
# types like images and masks.
verify_tensors = True


# Function to call when an exception is raised during node execution.
process_exception_logic = None


# Maximum number of times to retry a node if it fails. Useful for iterative debugging.
max_tries = 1


already_initialized = False
module_reload_times = {}
func_dict = {}


# For ComfyUI to pick up.
NODE_CLASS_MAPPINGS = {}
NODE_DISPLAY_NAME_MAPPINGS = {}


# Use as a default str value to show choices to the user.
class Choice(str):
    def __new__(cls, choices: list[str]):
        instance = super().__new__(cls, choices[0])
        instance.choices = choices
        return instance

    def __str__(self):
        return self.choices[0]


class StringInput(str):
    def __new__(cls, value, multiline=False, force_input=False, optional=False, hidden=False):
        instance = super().__new__(cls, value)
        instance.value = value
        instance.multiline = multiline
        instance.force_input = force_input
        instance.optional = optional
        instance.hidden = hidden
        return instance

    def to_dict(self):
        return {
            "default": self.value,
            "multiline": self.multiline,
            "display": "input",
            "forceInput": self.force_input,
        }


class NumberInput(float):
    def __new__(
        cls,
        default,
        min=None,
        max=None,
        step=None,
        round=None,
        display: str = "number",
        optional=False,
        hidden=False,
    ):
        if min is not None and default < min:
            raise ValueError(f"Value {default} is less than the minimum allowed {min}.")
        if max is not None and default > max:
            raise ValueError(
                f"Value {default} is greater than the maximum allowed {max}."
            )
        instance = super().__new__(cls, default)
        instance.min = min
        instance.max = max
        instance.display = display
        instance.step = step
        instance.round = round
        instance.optional = optional
        instance.hidden = hidden
        return instance

    def to_dict(self):
        metadata = {
            "default": self,
            "display": self.display,
            "min": self.min,
            "max": self.max,
            "step": self.step,
            "round": self.round,
        }
        metadata = {k: v for k, v in metadata.items() if v is not None}
        return metadata

    def __repr__(self):
        return f"{super().__repr__()} (Min: {self.min}, Max: {self.max})"


# Used for type hinting semantics only.
class ImageTensor(torch.Tensor):
    def __new__(cls):
        raise TypeError("Do not instantiate this class directly.")


# Used for type hinting semantics only.
class MaskTensor(torch.Tensor):
    def __new__(cls):
        raise TypeError("Do not instantiate this class directly.")


# Made to match any and all other types.
class AnyType(str):
    def __ne__(self, __value: object) -> bool:
        return False


any_type = AnyType("*")


_ANNOTATION_TO_COMFYUI_TYPE = {}
_SHOULD_AUTOCONVERT = {"str": True}
_DEFAULT_FORCE_INPUT = {}

def _get_fully_qualified_name(cls):
    return f"{cls.__module__}.{cls.__qualname__}"

def register_type(
    cls, name: str, 
    should_autoconvert: bool = False, 
    is_auto_register: bool = False, 
    force_input: bool = False
):
    """Register a type for ComfyUI annotations.

    Args:
        cls (type): The type to register.
        name (str): The name of the type.
        should_autoconvert (bool, optional): Whether the type should be automatically converted to the expected type before being passed to the wrapped function. Defaults to False.
        is_auto_register (bool, optional): Whether the type is automatically registered. Defaults to False.
        force_input (bool, optional): Whether the type should be forced as an input. Defaults to False.
    """
    key = _get_fully_qualified_name(cls)
    # if not is_auto_register:
    #     assert key not in _ANNOTATION_TO_COMFYUI_TYPE, f"Type {cls} already registered."

    if key in _ANNOTATION_TO_COMFYUI_TYPE:
        return

    _ANNOTATION_TO_COMFYUI_TYPE[key] = name
    _SHOULD_AUTOCONVERT[key] = should_autoconvert
    _DEFAULT_FORCE_INPUT[key] = force_input


register_type(torch.Tensor, "TENSOR")
register_type(ImageTensor, "IMAGE")
register_type(MaskTensor, "MASK")
register_type(int, "INT")
register_type(float, "FLOAT")
register_type(str, "STRING")
register_type(bool, "BOOLEAN")
register_type(AnyType, any_type)


def _get_type_str(the_type) -> str:
    key = _get_fully_qualified_name(the_type)
    if key not in _ANNOTATION_TO_COMFYUI_TYPE and get_origin(the_type) is list:
        return _get_type_str(get_args(the_type)[0])

    if key not in _ANNOTATION_TO_COMFYUI_TYPE and the_type is not inspect._empty:
        logging.warning(
            f"Type '{the_type}' not registered with ComfyUI, treating as wildcard"
        )

    type_str = _ANNOTATION_TO_COMFYUI_TYPE.get(key, any_type)
    return type_str


def _get_device():
    return torch.device("cuda:0" if torch.cuda.is_available() else "cpu")


def _add_preview(result):
     # Only import if we're running in the context of ComfyUI, so as to not
     # make ComfyUI a requirement for any code that happens to use ComfyUI-Annotations.
    import folder_paths
    assert len(result) >= 1, f"Expected at least 1 result, got {len(result)}"

    def get_first_result(results):
        if isinstance(results, tuple) or isinstance(results, list):
            if len(results) > 0:
                return get_first_result(results[0])
            else:
                return None
        # logging.info(f"Returning! {results}")
        return results
    
    preview_item = get_first_result(result) 
    if isinstance(preview_item, str):
        return {"ui": {"text": [preview_item]}, "result": result}
    elif isinstance(preview_item,  torch.Tensor):
        image = preview_item[0].cpu().numpy()
        image = Image.fromarray(np.clip(image * 255.0, 0, 255).astype(np.uint8))

        folder = Path(folder_paths.get_temp_directory())
        folder.mkdir(parents=True, exist_ok=True)
        filename = (
            "_temp_"
            + "".join(random.choice("abcdefghijklmnopqrstupvxyz") for x in range(5))
            + ".png"
        )
        full_output_path = folder / filename

        image.save(str(full_output_path), compress_level=4)        

        results = []
        results.append({"filename": filename, "subfolder": "", "type": "temp"})
        return {"ui": {"images": results}, "result": result}
    else:
        logging.warning("Result is not a string or tensor, not showing preview")
        return result


def _verify_tensor(arg, tensor_name="", tensor_type="", allowed_shapes=None, allowed_dims=None, allowed_channels=None) -> bool:
    if not verify_tensors:
        return True
    
    if isinstance(arg, torch.Tensor):
        if tensor_type == "MASK":
            assert arg.min() >= 0 and arg.max() <= 1, f"{tensor_name}: {tensor_type} tensor must have values between 0 and 1, got min {arg.min()} and max {arg.max()}"
        
        if allowed_shapes is not None:
            assert len(arg.shape) in allowed_shapes, f"{tensor_name}: {tensor_type} tensor must have shape in {allowed_shapes}, got {arg.shape}"
        if allowed_dims is not None:
            for i, dim in enumerate(allowed_dims):
                assert arg.shape[i] <= dim, f"{tensor_name}: {tensor_type} tensor dimension {i} must be less than or equal to {dim}, got {arg.shape[i]}"
        if allowed_channels is not None:
            assert arg.shape[-1] in allowed_channels, f"{tensor_name}: {tensor_type} tensor must have the number of channels in {allowed_channels}, got {arg.shape[-1]}"
    elif isinstance(arg, list):
        for a in arg:
            return _verify_tensor(a, tensor_name=tensor_name, allowed_shapes=allowed_shapes, allowed_dims=allowed_dims, allowed_channels=allowed_channels)


tensor_types = {
    "IMAGE": {"allowed_shapes": [4], "allowed_dims": None, "allowed_channels": [1, 3, 4]},
    "MASK": {"allowed_shapes": [3], "allowed_dims": None, "allowed_channels": None},
    "DEPTH": {"allowed_shapes": [4], "allowed_dims": None, "allowed_channels": [1]}
}


def _verify_tensor_type(key, arg, required_inputs, optional_inputs, tensor_name):
    for tensor_type, params in tensor_types.items():
        if (key in required_inputs and required_inputs[key][0] == tensor_type) or (key in optional_inputs and optional_inputs[key][0] == tensor_type):
            return _verify_tensor(arg, tensor_name=tensor_name, tensor_type=tensor_type, **params)
    return True


def _verify_return_type(ret, return_type, tensor_name):
    if return_type in tensor_types:
        return _verify_tensor(ret, tensor_name=tensor_name, tensor_type=return_type, **tensor_types[return_type])
    return True


def _all_to(device, arg):
    if isinstance(arg, torch.Tensor):
        return arg.to(device)
    elif isinstance(arg, list):
        return [_all_to(device, a) for a in arg]
    return arg


class ReturnInfo(Exception):
    def init(self, line_number):
        self.line_number = line_number


def _image_info(image: Union[torch.Tensor, np.ndarray], label: str=None) -> str:
    if isinstance(image, torch.Tensor):
        if image.dtype == torch.bool:
            return (f"shape={image.shape} dtype={image.dtype} device={image.device}")
        return (f"shape={image.shape} dtype={image.dtype} min={image.min()} max={image.max()}"
              + f" mean={image.mean()} sum={image.sum()} device={image.device}")
    elif isinstance(image, np.ndarray):
        return f"shape={image.shape}, dtype={image.dtype}, min={image.min()}, max={image.max()} mean={image.mean()} sum={image.sum()} "


class BufferHandler(logging.Handler):
    def __init__(self, buffer):
        logging.Handler.__init__(self)
        self.buffer = buffer
    
    def emit(self, record):
        msg = self.format(record)
        self.buffer.write(msg + '\n')


class Tee(object):
    def __init__(self, *files):
        self.files = files
    
    def write(self, obj):
        for f in self.files:
            f.write(obj)
    
    def flush(self):
        for f in self.files:
            f.flush()


def _maybe_reload_module(func: callable):
    module_name = func.__module__
    if not module_name or not reload_modules:
        return False, func
    
    module = importlib.import_module(module_name)
    module_file = module.__file__
    
    current_modified_time = os.path.getmtime(module_file)
    
    if current_modified_time > module_reload_times.get(module_name, 0):
        if hasattr(module, func.__name__):
            importlib.reload(module)
            logging.info(f"{Fore.GREEN}Reloaded module {module_name}{Fore.RESET}")
            module_reload_times[module_name] = current_modified_time
            func_dict[func.__name__] = getattr(module, func.__name__)
            return True, getattr(module, func.__name__)
    
    if func.__name__ in func_dict:
        return False, func_dict[func.__name__]
    
    return False, func


def _call_function_and_verify_result(func, args, kwargs, debug, input_desc, adjusted_return_types, wrapped_name, return_names=None):
    try_count = 0

    while try_count < max_tries:
        try_count += 1
        try:
            return_line_number = func.__code__.co_firstlineno
            node_logger = logging.getLogger()
            node_logger.setLevel(logging.INFO)
            buffer = io.StringIO()
            buffer_handler = BufferHandler(buffer)
            node_logger.addHandler(buffer_handler)
            buffer_handler.setFormatter(logging.Formatter('%(levelname)s: %(message)s'))
            sys.stdout = Tee(sys.stdout, buffer)

            logging.debug(f"-------- Running {wrapped_name} --------")
            result = func(*args, **kwargs)
            logging.debug(f"-------- Finished {wrapped_name} --------")

            code_origin_loc = f"\n Source: {func.__qualname__} {func.__code__.co_filename}:{return_line_number}"
            num_expected_returns = len(adjusted_return_types)
            if num_expected_returns == 0:
                assert result is None, f"{wrapped_name}: Return value is not None, but no return type specified.\n{code_origin_loc}"
                return (None,)

            if not isinstance(result, tuple):
                result = (result,)
            assert len(result) == len(
                adjusted_return_types
            ), f"{wrapped_name}: Number of return values {len(result)} does not match number of return types {len(adjusted_return_types)}\n{code_origin_loc}"

            for i, ret in enumerate(result):
                if ret is None:
                    logging.warning(f"Result {i} is None")

            new_result = _all_to("cpu", list(result))
            for i, ret in enumerate(result):
                if debug:
                    logging.info(f"Result {i} is {type(ret)}")
                try:
                    name = f"'{return_names[i]}'" if return_names else f"return_{i}"
                    _verify_return_type(ret, adjusted_return_types[i], name)
                except Exception as e:
                    raise ValueError(f"Error verifying OUTPUT tensor {str(e)}\n{code_origin_loc}") from None

            return tuple(new_result)

        except Exception as e:
            if try_count == max_tries:
                logging.info("Out of tries, raising exception")
                raise e
            
            if process_exception_logic:
                process_exception_logic(func, e, input_desc, buffer)

        finally:
            node_logger.removeHandler(buffer_handler)
            sys.stdout = sys.__stdout__
            if buffer:
                buffer.close()

    assert False, "Should never reach this point"



def ComfyFunc(
    category: str = default_category,
    display_name: str = None,
    workflow_name: str = None,
    description: str = None,
    is_output_node: bool = False,
    return_types: list = None,
    return_names: list[str] = None,
    validate_inputs: Callable = None,
    is_changed: Callable = None,
    has_preview: bool = False,
    debug: bool = False,
    color: str = None,
    automatic_debugging: bool = True,    
):
    """
    Decorator function for creating ComfyUI nodes.

    Args:
        category (str): The category of the node.
        display_name (str): The display name of the node. If not provided, it will be generated from the function name.
        workflow_name (str): The workflow name of the node. If not provided, it will be generated from the function name.
        is_output_node (bool): Indicates whether the node is an output node and should be run regardless of if anything depends on it.
        return_types (list): A list of types to return. If not provided, it will be inferred from the function's annotations.
        return_names (list[str]): The names of the outputs. Must match the number of return types.
        validate_inputs (Callable): A function used to validate the inputs of the node.
        is_changed (Callable): A function used to determine if the node's inputs have changed.
        debug (bool): Indicates whether to enable debug logging for this node.

    Returns:
        A callable used that can be used with a function to create a ComfyUI node.
    """
    def decorator(func):
        wrapped_name = func.__qualname__ + "_comfyfunc_wrapper"
        source_location = f"{func.__code__.co_filename}:{func.__code__.co_firstlineno}"
        code_origin_loc = f"\n Source: {func.__qualname__} {source_location}"
                
        if debug:
            logger = logging.getLogger(wrapped_name)
            logger.info(
                "-------------------------------------------------------------------"
            )
            logger.info(f"Decorating {func.__qualname__}")

        node_class = _get_node_class(func)

        is_static = _is_static_method(node_class, func.__name__)
        is_cls_mth = _is_class_method(node_class, func.__name__)
        is_member = node_class is not None and not is_static and not is_cls_mth

        required_inputs, hidden_inputs, optional_inputs, input_is_list_map, input_type_map = (
            _infer_input_types_from_annotations(func, is_member, debug)
        )

        if debug:
            logger.info(f"{func.__name__} Is static: {is_static} Is member: {is_member} Class method: {is_cls_mth}")
            logger.info(f"Required inputs: {required_inputs} optional: {optional_inputs} input_is_list: {input_is_list_map} input_type_map: {input_type_map}")

        adjusted_return_types = []
        output_is_list = []
        if return_types is not None:
            adjusted_return_types, output_is_list = _infer_return_types_from_annotations(
                return_types, debug
            )
        else:
            adjusted_return_types, output_is_list = _infer_return_types_from_annotations(func, debug)

        if return_names:
            assert len(return_names) == len(
                adjusted_return_types
            ), f"Number of output names must match number of return types. Got {len(return_names)} names and {len(return_types)} return types."

        # There's not much point in a node that doesn't have any outputs
        # and isn't an output itself, so auto-promote in that case.
        force_output = len(adjusted_return_types) == 0
        name_parts = [x.title() for x in func.__name__.split("_")]
        input_is_list = any(input_is_list_map.values())
        
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            if debug:
                logger.info(
                    f"Calling {func.__name__} with {len(args)} args and {len(kwargs)} kwargs. Is class method: {is_cls_mth}"
                )
                for i, arg in enumerate(args):
                    logger.info(f"arg {i}: {type(arg)}")
                for key, arg in kwargs.items():
                    logger.info(f"kwarg {key}: {type(arg)}")

            all_inputs = {**required_inputs, **optional_inputs}

            input_desc = []
            for key, arg in kwargs.items():
                arg = _all_to(_get_device(), arg)
                if (key in required_inputs and required_inputs[key][0] == "MASK"):
                    if len(arg.shape) == 2:
                        arg = arg.unsqueeze(0)

                if key in all_inputs:
                    cls = input_type_map[key]
                    if _SHOULD_AUTOCONVERT.get(_get_fully_qualified_name(cls), False):
                        if isinstance(arg, list):
                            arg = [cls(el) for el in arg]
                        else:
                            arg = cls(arg)
                
                desc_name = _get_fully_qualified_name(type(arg))
                if isinstance(arg, torch.Tensor):
                    input_desc.append(f"{key} ({desc_name}): {_image_info(arg)}")
                else:
                    input_desc.append(f"{key} ({desc_name}): {arg}")

                try:
                    _verify_tensor_type(
                        key, arg, required_inputs, optional_inputs, tensor_name=f"'{key}'"
                    )
                except Exception as e:
                    raise ValueError(f"Error verifying INPUT tensor {str(e)}\n{code_origin_loc}") from None
                
                kwargs[key] = arg

            # For some reason self still gets passed with class methods.
            if is_cls_mth:
                args = args[1:]

            # If the python function didn't annotate it as a list,
            # but INPUT_TYPES does, then we need to convert make it not a list.
            if input_is_list:
                for arg_name in kwargs.keys():
                    if debug:
                        print("kwarg:", arg_name, len(kwargs[arg_name]))
                    if not input_is_list_map[arg_name]:
                        assert len(kwargs[arg_name]) == 1
                        kwargs[arg_name] = kwargs[arg_name][0]
            
            global already_initialized
            already_initialized = True
            reloaded, latest_func = _maybe_reload_module(func)

            result = _call_function_and_verify_result(latest_func, args, kwargs, debug, input_desc, adjusted_return_types, wrapped_name,
                                                     return_names=return_names)

            if not has_preview:
                return result
            else:
                return _add_preview(result)

        if node_class is None or is_static:
            wrapper = staticmethod(wrapper)

        if is_cls_mth:
            wrapper = classmethod(wrapper)

        the_description = description
        if the_description is None:
            the_description = ""
            if docstring_mode is not AutoDescriptionMode.NONE and func.__doc__:
                the_description = func.__doc__.strip()
                if docstring_mode == AutoDescriptionMode.BRIEF:
                    the_description = the_description.split("\n")[0]

        _create_comfy_node(
            wrapped_name,
            category,
            node_class,
            wrapper,
            display_name if display_name else " ".join(name_parts),
            workflow_name if workflow_name else "".join(name_parts),
            required_inputs,
            hidden_inputs,
            optional_inputs,
            input_is_list,
            adjusted_return_types,
            return_names,
            output_is_list,
            description=the_description,
            is_output_node=is_output_node or force_output,
            validate_inputs=validate_inputs,
            is_changed=is_changed,
            color=color,
            debug=debug,
            source_location=source_location,
        )

        # Return the original function so it can still be used as normal (only ComfyUI sees the wrapper function).
        return func

    return decorator


def _annotate_input(
    annotation, default=inspect.Parameter.empty, debug=False
) -> tuple[tuple, bool, bool]:
    type_name = _get_type_str(annotation)
        
    if isinstance(default, Choice):
        return (default.choices,), False, False
    if isinstance(default, StringInput) or isinstance(default, NumberInput):
        return (type_name, default.to_dict()), default.optional, default.hidden

    metadata = {}
    if default is None:
        # If the user specified None explicitly, assume they're ok with it being optional.
        metadata["optional"] = True
        metadata["forceInput"] = True        
    elif default == inspect.Parameter.empty:
        # If they didn't give it a default value at all, then forceInput so that the UI
        # doesn't end up giving them a default that they may not want.
        metadata["forceInput"] = True
    else:
        metadata["default"] = default
    
    # This is the exception where they may have given it a default, but we still
    # want to force it as an input because changing that value will be rare.
    if _DEFAULT_FORCE_INPUT.get(_get_fully_qualified_name(annotation), False):
        metadata["forceInput"] = True

    return (type_name, metadata), default != inspect.Parameter.empty, False


def _infer_input_types_from_annotations(func, skip_first, debug=False):
    """
    Infer input types based on function annotations.
    """
    input_is_list = {}
    input_type_map = {}
    sig = inspect.signature(func)
    required_inputs = {}
    hidden_input_types = {}
    optional_input_types = {}

    params = list(sig.parameters.items())

    if debug:
        print("ALL PARAMS", params)

    if skip_first:
        if debug:
            print("SKIPPING FIRST PARAM ", params[0])
        params = params[1:]

    for param_name, param in params:
        origin = get_origin(param.annotation)
        input_is_list[param_name] = origin is list
        input_type_map[param_name] = param.annotation

        if debug:
            print("Param default:", param.default)

        the_param, is_optional, is_hidden = _annotate_input(param.annotation, param.default, debug)
        
        if param_name == "unique_id":
            hidden_input_types[param_name] = "UNIQUE_ID"
        elif param_name == "extra_pnginfo":
            hidden_input_types[param_name] = "EXTRA_PNGINFO"
        elif not is_optional:
            required_inputs[param_name] = the_param
        elif is_hidden:
            hidden_input_types[param_name] = the_param
        else:
            optional_input_types[param_name] = the_param
    return required_inputs, hidden_input_types, optional_input_types, input_is_list, input_type_map


def _infer_return_types_from_annotations(func_or_types, debug=False):
    """
    Infer whether each element in a function's return tuple is a list or a single item,
    handling direct list inputs as well as function annotations.
    """
    if isinstance(func_or_types, list):
        # Direct list of types provided
        return_args = func_or_types
        origin = tuple  # Assume tuple if directly provided with a list
    else:
        # Assuming it's a function, inspect its return annotation
        return_annotation = inspect.signature(func_or_types).return_annotation
        return_args = get_args(return_annotation)
        origin = get_origin(return_annotation)

        if debug:
            print(f"return_annotation: '{return_annotation}'")
            print(f"return_args: '{return_args}'")
            print(f"origin: '{origin}'")
            print(type(return_annotation), return_annotation)

    types_mapped = []
    output_is_list = []

    if origin is tuple:
        for arg in return_args:
            if get_origin(arg) == list:
                output_is_list.append(True)
                list_arg = get_args(arg)[0]
                types_mapped.append(_get_type_str(list_arg))
            else:
                output_is_list.append(False)
                types_mapped.append(_get_type_str(arg))
    elif origin is list:
        if debug:
            print(_get_type_str(return_annotation))
            print(return_annotation)
            print(return_args)
        types_mapped.append(_get_type_str(return_args[0]))
        output_is_list.append(origin is list)
    elif return_annotation is not inspect.Parameter.empty:
        types_mapped.append(_get_type_str(return_annotation))
        output_is_list.append(False)

    return_types_tuple = tuple(types_mapped)
    output_is_lists_tuple = tuple(output_is_list)
    if debug:
        print(
            f"return_types_tuple: '{return_types_tuple}', output_is_lists_tuple: '{output_is_lists_tuple}'"
        )

    return return_types_tuple, output_is_lists_tuple


def _create_comfy_node(
    cname,
    category,
    node_class,
    process_function,
    display_name,
    workflow_name,
    required_inputs,
    hidden_inputs,
    optional_inputs,
    input_is_list,
    return_types,
    return_names,
    output_is_list,
    description=None,
    is_output_node=False,
    validate_inputs=None,
    is_changed=None,
    color=None,
    source_location=None,
    debug=False,
):
    all_inputs = {"required": required_inputs, "hidden": hidden_inputs, "optional": optional_inputs}
      
    # Smuggle the color in with the description. Bit of a hack,
    # but afaict there's no way to pass it directly.
    if color is not None:
        assert color.startswith("#"), "Color must be a hex color code"
        assert len(color) == 7, "Color must be a hex color code"
        assert all(c in "0123456789ABCDEF" for c in color[1:]), "Color must be a hex color code"
        
        # Turn it into floating point
        color = color[1:]
        color = [int(color[i:i+2], 16) / 255.0 for i in (0, 2, 4)]
        bgcolor = [c * 0.6 for c in color]
        hex_bg_color = "#" + "".join(f"{int(c * 255):02X}" for c in bgcolor)
        
        description += f"\n\nComfyUINodeColor={color}\nComfyUINodeBgColor={hex_bg_color}"

    if source_location is not None:
        description += f"\n\nNodeSource={source_location}"

    # Initial class dictionary setup
    class_dict = {
        "INPUT_TYPES": classmethod(lambda cls: all_inputs),
        "CATEGORY": category,
        "RETURN_TYPES": return_types,
        "FUNCTION": cname,
        "INPUT_IS_LIST": input_is_list,
        "OUTPUT_IS_LIST": output_is_list,
        "OUTPUT_NODE": is_output_node,
        "RETURN_NAMES": return_names,
        "VALIDATE_INPUTS": validate_inputs,
        "IS_CHANGED": is_changed,
        "DESCRIPTION": description,
        cname: process_function,
    }
    class_dict = {k: v for k, v in class_dict.items() if v is not None}

    if debug:
        logger = logging.getLogger(cname)
        for key, value in class_dict.items():
            logger.info(f"{key}: {value}")

    if not already_initialized:
        assert (
            workflow_name not in NODE_CLASS_MAPPINGS
        ), f"Node class '{workflow_name} ({cname})' already exists!"
        assert (
            display_name not in NODE_DISPLAY_NAME_MAPPINGS.values()
        ), f"Display name '{display_name}' already exists!"
        assert (
            node_class not in NODE_CLASS_MAPPINGS.values()
        ), f"Only one method from '{node_class} can be used as a ComfyUI node.'"

    if node_class:
        for key, value in class_dict.items():
            setattr(node_class, key, value)
    else:
        node_class = type(workflow_name, (object,), class_dict)

    NODE_CLASS_MAPPINGS[workflow_name] = node_class
    NODE_DISPLAY_NAME_MAPPINGS[workflow_name] = display_name


def _is_static_method(cls, attr):
    """Check if a method is a static method."""
    if cls is None:
        return False
    attr_value = inspect.getattr_static(cls, attr, None)
    is_static = isinstance(attr_value, staticmethod)
    return is_static


def _is_class_method(cls, attr):
    if cls is None:
        return False
    attr_value = inspect.getattr_static(cls, attr, None)
    is_class_method = isinstance(attr_value, classmethod)
    return is_class_method


def _get_node_class(func):
    split_name = func.__qualname__.split(".")

    if len(split_name) > 1:
        class_name = split_name[-2]
        node_class = globals().get(class_name, None)
        if node_class is None and hasattr(func, "__globals__"):
            node_class = func.__globals__.get(class_name, None)
        return node_class
    return None


T = typing.TypeVar("T")


def register_setter(cls: type, category=default_category, extra_imports: list[str] = [], debug=False) -> typing.Callable[..., T]:
    if debug:
        logging.info(f"Registering setter for class '{cls.__name__}'")
    register_type(cls, name=cls.__name__)
    ComfyFunc(category, display_name=cls.__name__, workflow_name=cls.__name__, debug=debug)(
        create_dynamic_setter(cls, extra_imports=extra_imports))


def create_dynamic_setter(cls: type, extra_imports: list[str] = [], debug=False) -> typing.Callable[..., T]:
    obj = cls()
    func_name = cls.__name__
    setter_name = func_name + "_setter"
    return_type = f"{cls.__module__}.{cls.__name__}"

    properties = {}
    module_names = set()

    # Collect properties and infer types from their current instantiated values
    for attr_name in dir(cls):
        attr = getattr(cls, attr_name, None)
        if attr and isinstance(attr, property) and attr.fset is not None:
            current_value = getattr(obj, attr_name, None)
            prop_type = type(current_value) if current_value is not None else typing.Any
            properties[attr_name] = (prop_type, current_value)

            if debug:
                logging.info(
                    f"Property '{attr_name}' has type '{prop_type}' and value '{current_value}'"
                )

            # Automatically register the type and its subtypes, allowing duplicates
            register_type(
                prop_type, _get_fully_qualified_name(prop_type), is_auto_register=True
            )
            if hasattr(prop_type, "__args__"):
                for subtype in prop_type.__args__:
                    register_type(
                        subtype,
                        _get_fully_qualified_name(subtype),
                        is_auto_register=True,
                    )

            # Extract module name from the property type
            module_name = _get_fully_qualified_name(prop_type).rsplit(".", 1)[0]
            if "." in module_name:
                module_names.add(module_name)

    # Extract module name from the return type
    return_module = return_type.rsplit(".", 1)[0]
    if "." in return_module:
        module_names.add(return_module)

    func_params = []
    for prop, (prop_type, current_value) in properties.items():
        if prop_type in [int, float]:
            func_params.append(
                f"{prop}: {_get_fully_qualified_name(prop_type).replace('builtins.', '')}=NumberInput({current_value})"
            )
        elif prop_type == str:
            func_params.append(
                f"{prop}: {_get_fully_qualified_name(prop_type).replace('builtins.', '')}=StringInput('{current_value}')"
            )
        elif prop_type == bool:
            func_params.append(
                f"{prop}: {_get_fully_qualified_name(prop_type).replace('builtins.', '')}={current_value}"
            )
        else:
            func_params.append(
                f"{prop}: {_get_fully_qualified_name(prop_type).replace('builtins.', '')}=None"
            )

    func_params_str = ", ".join(func_params)

    # Generate import statements
    import_statements = [
        "import typing",
        *[f"import {module_name}" for module_name in module_names],
        "import importlib",
        "from comfy_annotations import NumberInput, StringInput",
    ]
    
    for extra_import in extra_imports:
        import_statements.append(f"import {extra_import}")

    func_body_lines = [
        f"module = importlib.import_module('{return_module}')",
        f"cls = getattr(module, '{func_name}')",
        "new_obj = cls()",
        *[
            f"if {prop} is not None: setattr(new_obj, '{prop}', {prop})"
            for prop in properties.keys()
        ],
        "return new_obj",
    ]
    func_body = "\n    ".join(func_body_lines)
    func_code = (
        "\n".join(import_statements)
        + f"\n\ndef {setter_name}({func_params_str}) -> {return_type}:\n    {func_body}"
    )

    if debug:
        logging.info(f"Creating dynamic setter with code: '{func_code}'")

    globals_dict = {
        "typing": typing,
        "importlib": importlib,
        "NumberInput": NumberInput,
        "StringInput": StringInput,
    }
    locals_dict = {}

    # Update the global namespace with the module names
    for module_name in module_names:
        globals_dict[module_name] = importlib.import_module(module_name)

    # Execute the function code
    exec(func_code, globals_dict, locals_dict)

    # Get the function object from the local namespace
    func = locals_dict[setter_name]

    return func

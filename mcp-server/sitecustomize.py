"""Allow Home Assistant YAML tags during syntax validation.

PyYAML's SafeLoader rejects Home Assistant tags such as !include and !secret.
For validation we only need to verify YAML structure, not execute those tags.
"""

import yaml
from yaml.nodes import MappingNode, ScalarNode, SequenceNode


def _construct_home_assistant_tag(loader, tag_suffix, node):
    if isinstance(node, ScalarNode):
        return loader.construct_scalar(node)
    if isinstance(node, SequenceNode):
        return loader.construct_sequence(node)
    if isinstance(node, MappingNode):
        return loader.construct_mapping(node)
    return None


yaml.SafeLoader.add_multi_constructor("!", _construct_home_assistant_tag)

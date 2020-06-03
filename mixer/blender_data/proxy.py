from __future__ import annotations

import array
from contextlib import contextmanager
from dataclasses import dataclass
from enum import IntEnum
import logging
from typing import Any, Mapping, Union, Set, List, Tuple, TypeVar
from uuid import uuid4

import bpy
import bpy.types as T  # noqa
import mathutils

from mixer.blender_data import specifics
from mixer.blender_data.filter import Context, safe_depsgraph_updates
from mixer.blender_data.blenddata import (
    BlendData,
    collection_name_to_type,
    rna_identifier_to_collection_name,
    bl_rna_to_type,
)
from mixer.blender_data.types import is_builtin, is_vector, is_matrix

BpyBlendDiff = TypeVar("BpyBlendDiff")
BpyIDProxy = TypeVar("BpyIDProxy")

logger = logging.Logger(__name__, logging.INFO)

# Access path to the ID starting from bpy.data, such as ("cameras", "Camera")
# or ("cameras", "Camera", "dof", "focus_object").
BlenddataPath = Tuple[str]


# storing everything in a single dictionary is easier for serialization
MIXER_SEQUENCE = "__mixer_sequence__"


def debug_check_stack_overflow(func, *args, **kwargs):
    """
    Use as a function decorator to detect probable stack overflow in case of circular references

    Beware : inspect performance is very poor.
    sys.setrecursionlimit cannot be used because it will possibly break the Blender VScode
    plugin and StackOverflowException is not caught by VScode "Raised exceptions" breakpoint.
    """

    def wrapper(*args, **kwargs):
        import inspect

        if len(inspect.stack(0)) > 50:
            raise RuntimeError("Possible stackoverflow")
        return func(*args, **kwargs)

    return wrapper


# Using a context doubles load time in SS2.82. This remains true for a null context
# Remmoving the "with" lines halves loading time (!!)
class DebugContext:
    """
    Context class only used during BpyBlendProxy construction, to keep contextual data during traversal
    of the blender data hierarchy and perform safety checkes
    """

    serialized_addresses: Set[bpy.types.ID] = set()  # Already serialized addresses (struct or IDs), for debug
    property_stack: List[Tuple[str, any]] = []  # Stack of properties up to this point in the visit
    property_value: Any

    @contextmanager
    def enter(self, property_name, property_value):
        self.property_stack.append((property_name, property_value))
        yield
        self.property_stack.pop()
        self.serialized_addresses.add(id(property_value))

    def visit_depth(self):
        # Utility for debug
        return len(self.property_stack)

    def property_fullpath(self):
        # Utility for debug
        return ".".join([p[0] for p in self.property_stack])


# TODO useless after IDProxies addition
# Warning, unusable to retrieve proxies from depsgraph update
RootIds = Set[T.ID]
IDProxies = Mapping[str, BpyIDProxy]
IDs = Mapping[str, T.ID]


@dataclass
class VisitState:
    root_ids: RootIds
    id_proxies: IDProxies
    ids: IDs
    context: Context
    debug_context: DebugContext = DebugContext()


class LoadElementAs(IntEnum):
    STRUCT = 0
    ID_REF = 1
    ID_DEF = 2


def same_rna(a, b):
    return a.bl_rna == b.bl_rna


def is_ID_subclass_rna(bl_rna):  # noqa
    """
    Return true if the RNA is of a subclass of bpy.types.ID
    """
    return issubclass(bl_rna_to_type(bl_rna), bpy.types.ID)


def load_as_what(attr_property: bpy.types.Property, attr: any, root_ids: RootIds):
    """
    Determine if we must load an attribute as a struct, a blenddata collection element (ID_DEF)
    or a reference to a BlendData collection element (ID_REF)

    All T.Struct are loaded as struct
    All T.ID are loaded ad IDRef (that is pointer into a D.blendata collection) except
    for specific case. For instance the scene master "collection" is not a D.collections item.

    Arguments
    parent -- the type that contains the attribute names attr_name, for instance T.Scene
    attr_property -- a bl_rna property of a attribute, that can be a CollectionProperty or a "plain" attribute
    """
    # Reference to an ID element at the root of the blend file
    if attr in root_ids:
        return LoadElementAs.ID_REF

    if same_rna(attr_property, T.CollectionProperty):
        if is_ID_subclass_rna(attr_property.fixed_type.bl_rna):
            # Collections at root level are not handled by this code (See BpyBlendProxy.load()) so here it
            # should be a nested collection and IDs should be ref to root collections.
            return LoadElementAs.ID_REF
        else:
            # Only collections at the root level of blendfile are id def, so here it can only be struct
            return LoadElementAs.STRUCT

    if same_rna(attr_property, T.PointerProperty):
        element_property = attr_property.fixed_type
    else:
        element_property = attr_property

    if is_ID_subclass_rna(element_property.bl_rna):
        return LoadElementAs.ID_DEF

    return LoadElementAs.STRUCT


# @debug_check_stack_overflow
def read_attribute(attr: any, attr_property: any, visit_state: VisitState):
    """
    Load a property into a python object of the appropriate type, be it a Proxy or a native python object


    """
    with visit_state.debug_context.enter(attr_property.identifier, attr):
        attr_type = type(attr)

        if is_builtin(attr_type):
            return attr
        if is_vector(attr_type):
            return list(attr)
        if is_matrix(attr_type):
            return [list(col) for col in attr.col]

        # We have tested the types that are usefully reported by the python binding, now harder work.
        # These were implemented first and may be better implemented with the bl_rna property of the parent struct
        if attr_type == T.bpy_prop_array:
            return [e for e in attr]

        if attr_type == T.bpy_prop_collection:
            # need to know for each element if it is a ref or id
            load_as = load_as_what(attr_property, attr, visit_state.root_ids)
            if load_as == LoadElementAs.STRUCT:
                return BpyPropStructCollectionProxy().load(attr, attr_property, visit_state)
            elif load_as == LoadElementAs.ID_REF:
                # References into Blenddata collection, for instance D.scenes[0].objects
                return BpyPropDataCollectionProxy().load_as_IDref(attr, visit_state)
            elif load_as == LoadElementAs.ID_DEF:
                # is  BlendData collection, for instance D.objects
                return BpyPropDataCollectionProxy().load_as_ID(attr, visit_state)

        # TODO merge with previous case
        if isinstance(attr_property, T.CollectionProperty):
            return BpyPropStructCollectionProxy().load(attr, attr_property, visit_state)

        bl_rna = attr_property.bl_rna
        if bl_rna is None:
            logger.warning("Not implemented: attribute %s", attr)
            return None

        assert issubclass(attr_type, T.PropertyGroup) == issubclass(attr_type, T.PropertyGroup)
        if issubclass(attr_type, T.PropertyGroup):
            return BpyPropertyGroupProxy().load(attr, visit_state)

        load_as = load_as_what(attr_property, attr, visit_state.root_ids)
        if load_as == LoadElementAs.STRUCT:
            return BpyStructProxy().load(attr, visit_state)
        elif load_as == LoadElementAs.ID_REF:
            return BpyIDRefProxy().load(attr, visit_state)
        elif load_as == LoadElementAs.ID_DEF:
            return BpyIDProxy().load(attr, visit_state)

        # assert issubclass(attr_type, T.bpy_struct) == issubclass(attr_type, T.bpy_struct)
        raise AssertionError("unexpected code path")
        # should be handled above
        if issubclass(attr_type, T.bpy_struct):
            return BpyStructProxy().load(attr, visit_state)

        raise ValueError(f"Unsupported attribute type {attr_type} without bl_rna for attribute {attr} ")


class Proxy:
    def __eq__(self, other):
        if self.__class__ is not other.__class__:
            return False
        if len(self._data) != len(other._data):
            return False

        for k, v in self._data.items():
            if k not in other._data.keys():
                return False
            if v != other._data[k]:
                return False
        return True

    def data(self, key):
        if isinstance(key, int):
            return self._data[MIXER_SEQUENCE][key]
        else:
            return self._data[key]

    def save(self, bl_instance: any, attr_name: str):
        """
        Save this proxy into a blender object
        """
        logging.warning(f"Not implemented: save() for {self.__class__} {bl_instance}.{attr_name}")


class StructLikeProxy(Proxy):
    """
    Holds a copy of a Blender bpy_struct
    """

    # TODO limit depth like in multiuser. Anyhow, there are circular references in f-curves
    def __init__(self):

        # We care for some non readonly properties. Collection object are tagged read_only byt can be updated with

        # Beware :

        # >>> bpy.types.Scene.bl_rna.properties['collection']
        # <bpy_struct, PointerProperty("collection")>

        # TODO is_readonly may be only interesting for "base types". FOr Collections it seems always set to true
        # meaning that the collection property slot cannot be updated although the object is mutable
        # TODO we also care for some readonly properties that are in fact links to data collections

        # The property information are taken from the containing class, not from the attribute.
        # So we get :
        #   T.Scene.bl_rna.properties['collection']
        #       <bpy_struct, PointerProperty("collection")>
        #   T.Scene.bl_rna.properties['collection'].fixed_type
        #       <bpy_struct, Struct("Collection")>
        # But if we take the information in the attribute we get information for the dereferenced
        # data
        #   D.scenes[0].collection.bl_rna
        #       <bpy_struct, Struct("Collection")>
        #
        # We need the former to make a difference between T.Scene.collection and T.Collection.children.
        # the former is a pointer
        self._data = {}
        pass

    def load(self, bl_instance: any, visit_state: VisitState):

        """
        Load a Blender object into this proxy
        """
        self._data.clear()
        for name, bl_rna_property in visit_state.context.properties(bl_instance):
            attr = getattr(bl_instance, name)
            attr_value = read_attribute(attr, bl_rna_property, visit_state)
            # Also write None values. We use them to reset attributes like Camera.dof.focus_object
            self._data[name] = attr_value
        return self

    def save(self, bl_instance: any, key: Union[int, str]):
        """
        Save this proxy into a Blender attribute
        """
        assert isinstance(key, int) or isinstance(key, str)
        if isinstance(key, int):
            target = bl_instance[key]
        elif isinstance(bl_instance, T.bpy_prop_collection):
            # TODO append an element :
            # https://blenderartists.org/t/how-delete-a-bpy-prop-collection-element/642185/4
            target = bl_instance.get(key)
        else:
            target = getattr(bl_instance, key)

        if target is None:
            logging.warning(f"Cannot write to '{bl_instance}', attribute '{key}' because it does not exist")
            return

        for k, v in self._data.items():
            write_attribute(target, k, v)


class BpyPropertyGroupProxy(StructLikeProxy):
    pass


class BpyStructProxy(StructLikeProxy):
    pass


class BpyIDProxy(BpyStructProxy):
    """
    Holds a copy of a Blender ID, i.e a type stored in bpy.data, like Object and Material
    """

    _blenddata_path: BlenddataPath = None

    # Addtitional arguments for the BlendDataXxxx collections object addtition(.new(), .load(), ...), besides name
    # For instance object_data in BlendDataObject.new(name, object_data)
    _ctor_args: List[Any] = None
    # Has a value if this proxy comes from a bpy.data ID
    mixer_uuid: str = None

    def __init__(self):
        super().__init__()

    def load(self, bl_instance: T.ID, visit_state: VisitState, blenddata_path: BlenddataPath = None):
        """
        """

        # TODO check that bl_instance class derives from ID
        super().load(bl_instance, visit_state)

        if blenddata_path is not None:
            self._blenddata_path = blenddata_path
        self._ctor_args = specifics.ctor_args(bl_instance, self)

        uuid = bl_instance.mixer_uuid
        if uuid:
            # It is a bpy.data ID, not an ID "embedded" inside another ID, like scene.collection
            id_ = visit_state.ids.get(uuid)
            if id_ is not bl_instance:
                # this occurs when
                # - when we find a reference to a BlendData ID that was not loaded
                # - the ID are not properly ordred at creation time, for instance (objects, meshes)
                # instead of (meshes, objects) : a bug
                logger.debug("BpyIDProxy.load(): %s not in visit_state.ids[uuid]", bl_instance)
            self.mixer_uuid = uuid
            visit_state.id_proxies[uuid] = self
        return self

    def collection_name(self) -> Union[str, None]:
        return self._blenddata_path[0]

    def collection_key(self) -> Union[str, None]:
        return self._blenddata_path[1]

    def target(self, bl_instance: any, attr_name: str):
        if isinstance(bl_instance, bpy.types.bpy_prop_collection):
            # TODO : slow: use BlendData
            t = bl_instance.get(attr_name)
            if t is None:
                logger.warning(
                    f"BpyIDProxy: key '{attr_name}'' not found in bpy_prop_collection {bl_instance}. Valid keys are ... {bl_instance.keys()}"
                )
        else:
            t = getattr(bl_instance, attr_name)
        return t

    def pre_save(self, bl_instance: any, attr_name: str):
        """
        Process attributes that must be saved first and return a possibily updated reference to the target
        """
        target = self.target(bl_instance, attr_name)

        if isinstance(target, bpy.types.Scene):
            # Set 'use_node' to True first is the only way I know to be able to set the 'node_tree' attribute
            use_nodes = self._data.get("use_nodes")
            if use_nodes:
                write_attribute(target, "use_nodes", True)
            sequence_editor = self._data.get("sequence_editor")
            if sequence_editor is not None and target.sequence_editor is None:
                target.sequence_editor_create()
        elif isinstance(target, bpy.types.Light):
            # required first to have access to new light type attributes
            light_type = self._data.get("type")
            if light_type is not None and light_type != target.type:
                write_attribute(target, "type", light_type)
                # must reload the reference
                target = self.target(bl_instance, attr_name)
        elif isinstance(target, bpy.types.MetaBall):
            # required first enable write on texspace_location and texspace_size if False
            # otherwise issues an error message
            # TODO useless on its own : strip values on load
            name = "use_auto_texspace"
            value = self._data.get(name)
            if value is not None and value != getattr(target, name):
                write_attribute(target, name, value)
        return target

    def save(self, bl_instance: any = None, attr_name: str = None):
        """
        - bl_instance: the container attribute
        """
        blenddata = BlendData.instance()
        if self._blenddata_path is None:
            # TODO ID inside ID, not BlendData collection. We will need a path
            # do it on save
            logging.warning(f"BpyIDProxy.save() {bl_instance}.{attr_name} has empty blenddata path: ignore")
            return

        if bl_instance is None:
            collection_name = self._blenddata_path[0]
            bl_instance = blenddata.bpy_collection(collection_name)
        if attr_name is None:
            attr_name = self._blenddata_path[1]

        # 2 cases
        # - ID in blenddata collection
        # - ID in ID
        # TODO slow use blenddata, redundant with target
        id_ = bl_instance.get(attr_name)
        if id_ is None:
            # assume ID in Blendata collection

            # unwrap the ctor arguments. For instance, creating an Object requires a T.ID
            ctor_args = []
            for arg in self._ctor_args or ():
                if isinstance(arg, BpyIDRefProxy):
                    data_collection_name, data_key = arg._blenddata_path
                    id_ = BlendData.instance().collection(data_collection_name)[data_key]
                    ctor_args.append(id_)
                else:
                    ctor_args.append(arg)
            blenddata.collection(collection_name).ctor(attr_name, ctor_args)

        target = self.pre_save(bl_instance, attr_name)
        if target is None:
            logging.warning(f"BpyIDProxy.save() {bl_instance}.{attr_name} is None")
            return

        for k, v in self._data.items():
            write_attribute(target, k, v)


class BpyIDRefProxy(Proxy):
    """
    A reference to an item of bpy_prop_collection in bpy.data member
    """

    _blenddata_path: BlenddataPath = None

    def __init__(self):
        self._data = {}

    def load(self, bl_instance, visit_state: VisitState):
        # Nothing to filter here, so we do not need the context/filter

        # Walk up to child of ID
        class_bl_rna = bl_instance.bl_rna
        while class_bl_rna.base is not None and class_bl_rna.base != bpy.types.ID.bl_rna:
            class_bl_rna = class_bl_rna.base

        # Safety check that the instance can really be accessed from root collections with its full name
        assert getattr(bpy.data, rna_identifier_to_collection_name[class_bl_rna.identifier], None) is not None
        assert bl_instance.name_full in getattr(bpy.data, rna_identifier_to_collection_name[class_bl_rna.identifier])

        self._blenddata_path = (
            BlendData.instance().bl_collection_name_from_inner_identifier(class_bl_rna.identifier),
            bl_instance.name_full,
        )
        return self

    def collection(self):
        return self._blenddata_path[0]

    def key(self):
        return self._blenddata_path[1]

    def save(self, bl_instance: any, attr_name: str):
        """
        Save this proxy into bl_instance.attr_name
        """

        # Cannot save into an attribute, which looks like an r-value.
        # When setting my_scene.world wen must setattr(scene, "world", data) and cannot
        # assign scene.world
        collection_name, collection_key = self._blenddata_path

        # TODO the identifier garget doest not have the same semantics everywhere
        # here pointee somewhere else lvalue
        # TODO use BlendData
        target = getattr(bpy.data, collection_name)[collection_key]
        if isinstance(bl_instance, T.bpy_prop_collection):
            logging.warning(f"Not implemented: BpyIDRefProxy.save() for IDRef into collection {bl_instance}{attr_name}")
        else:
            if not bl_instance.bl_rna.properties[attr_name].is_readonly:
                try:
                    setattr(bl_instance, attr_name, target)
                except Exception as e:
                    logging.warning(f"write attribute skipped {attr_name} for {bl_instance}...")
                    logging.warning(f" ...Error: {repr(e)}")


def ensure_uuid(item: bpy.types.ID) -> str:
    uuid = item.get("mixer_uuid")
    if uuid is None:
        item.mixer_uuid = str(uuid4())
    return uuid


# in sync with soa_initializers
soable_properties = {
    T.BoolProperty,
    T.IntProperty,
    T.FloatProperty,
    mathutils.Vector,
    mathutils.Color,
    mathutils.Quaternion,
}

# in sync with soa_initializers
soa_initializers = {
    bool: [False],
    int: array.array("l", [0]),
    float: array.array("f", [0.0]),
    mathutils.Vector: array.array("f", [0.0]),
    mathutils.Color: array.array("f", [0.0]),
    mathutils.Quaternion: array.array("f", [0.0]),
}


# TODO : is there any way to find these automatically ? Seems easy to determine if a struct is simple enough so that
# an array of struct can be loaded as an Soa. Is it worth ?
# Beware that MeshVertex must be handled as SOA although "groups" is a variable length item.
# Enums are not handled by foreach_get()
soable_collection_properties = {
    T.GPencilStroke.bl_rna.properties["points"],
    T.GPencilStroke.bl_rna.properties["triangles"],
    T.Mesh.bl_rna.properties["vertices"],
    T.Mesh.bl_rna.properties["edges"],
    T.Mesh.bl_rna.properties["loops"],
    # messy: :MeshPolygon.vertices has variable length, not 3 as stated in the doc, so ignore
    # T.Mesh.bl_rna.properties["polygons"],
    T.MeshUVLoopLayer.bl_rna.properties["data"],
    T.MeshLoopColorLayer.bl_rna.properties["data"],
}


def is_soable_collection(prop):
    return prop in soable_collection_properties


def is_soable_property(bl_rna_property):
    return any(isinstance(bl_rna_property, soable) for soable in soable_properties)


def soa_initializer(attr_type, length):
    # According to bpy_rna.c:foreach_getset() and rna_access.c:rna_raw_access() implementations,
    # some cases are implemented as memcpy (buffer interface) or array iteration (sequences),
    # with more subcases that require reallocation when the buffer type is not suitable,
    # so try to be smart
    element_init = soa_initializers[attr_type]
    if isinstance(element_init, array.array):
        return array.array(element_init.typecode, element_init.tolist() * length)
    elif isinstance(element_init, list):
        return element_init * length


class AosElement(Proxy):
    """
    A structure member inside a bpy_prop_collection loaded as a structure of array element

    For instance, MeshVertex.groups is a bpy_prop_collection of variable size and it cannot
    be loaded as an Soa in Mesh.vertices. So Mesh.vertices loads a "groups" AosElement
    """

    def __init__(self):
        self._data: Mapping[str, List] = {}

    def load(
        self, bl_collection: bpy.types.bpy_prop_collection, item_bl_rna, attr_name: str, visit_state: VisitState,
    ):
        """
        - bl_collection: a collection of structure, e.g. T.Mesh.vertices
        - item_bl_rna: the bl_rna if the structure contained in the collection, e.g. T.MeshVertices.bl_rna
        - attr_name: a member if the structure to be loaded as a sequence, e.g. "groups"
        """

        logging.warning(f"Not implemented. Load AOS  element for {bl_collection}.{attr_name} ")
        return self

        # This was written for MeshVertex.groups, but MeshVertex.groups is updated via Object.vertex_groups so
        # its is useless in this case.
        # Any other usage ?

        # self._data.clear()
        # attr_property = item_bl_rna.properties[attr_name]
        # # A bit overkill:
        # # for T.Mesh.vertices[...].groups, generates a BpyPropStructCollectionProxy per Vertex even if empty
        # self._data[MIXER_SEQUENCE] = [
        #     read_attribute(getattr(item, attr_name), attr_property, context, visit_context) for item in bl_collection
        # ]
        # return self

    def save(self, bl_collection: bpy.types.bpy_prop_collection, attr_name: str):

        logging.warning(f"Not implemented. Save AOS  element for {bl_collection}.{attr_name} ")

        # see comment in load()

        # sequence = self._data.get(MIXER_SEQUENCE)
        # if sequence is None:
        #     return

        # if len(sequence) != len(bl_collection):
        #     # Avoid by writing SOA first ? Is is enough to resize the target
        #     logging.warning(
        #         f"Not implemented. Save AO size mistmatch (incoming {len(sequence)}, target {len(bl_collection)}for {bl_collection}.{attr_name} "
        #     )
        #     return

        # for i, value in enumerate(sequence):
        #     target = bl_collection[i]
        #     write_attribute(target, attr_name, value)


class SoaElement(Proxy):
    """
    A structure member inside a bpy_prop_collection loaded as a structure of array element

    For instance, Mesh.vertices[].co is loaded as an SoaElement of Mesh.vertices. Its _data is an array
    """

    def __init__(self):
        self._data: Union[array.array, [List]] = None

    def load(self, bl_collection: bpy.types.bpy_prop_collection, attr_name: str, prototype_item):
        attr = getattr(prototype_item, attr_name)
        attr_type = type(attr)
        length = len(bl_collection)
        if is_vector(attr_type):
            length *= len(attr)
        elif attr_type is T.bpy_prop_array:
            length *= len(attr)
            attr_type = type(attr[0])

        buffer = soa_initializer(attr_type, length)

        # "RuntimeError: internal error setting the array" means that the array is ill-formed.
        # Check rna_access.c:rna_raw_access()
        bl_collection.foreach_get(attr_name, buffer)
        self._data = buffer
        return self

    def save(self, bl_collection, attr_name):
        bl_collection.foreach_set(attr_name, self._data)


# TODO make his generic after enough sequences have bee seen
def write_curvemappoints(target, src_sequence):
    src_length = len(src_sequence)

    # CurveMapPoints specific (alas ...)
    if src_length < 2:
        logging.error(f"Invalid length for curvemap: {src_length}. Expected at least 2")
        return

    # truncate dst
    while src_length < len(target):
        target.remove(target[-1])

    # extend dst
    while src_length > len(target):
        # .new() parameters are CurveMapPoints specific
        # for CurvamapPoint, we can initilaize to anything then overwrite. Not sure this is doable for other types
        # new inserts in head !
        # Not optimal for big arrays, but much simpler given that the new() parameters depend on the collection
        # in a way that cannot be determined automatically
        target.new(0.0, 0.0)

    assert src_length == len(target)
    for i in range(src_length):
        write_attribute(target, i, src_sequence[i])


def write_metaballelements(target, src_sequence):
    src_length = len(src_sequence)

    # truncate dst
    while src_length < len(target):
        target.remove(target[-1])

    # extend dst
    while src_length > len(target):
        # Creates a BALL, but will be changed by write_attribute
        target.new()

    assert src_length == len(target)
    for i in range(src_length):
        write_attribute(target, i, src_sequence[i])


class BpyPropStructCollectionProxy(Proxy):
    """
    Proxy to a bpy_prop_collection of non-ID in bpy.data
    """

    def __init__(self):
        self._data: Mapping[Union[str, int], BpyIDProxy] = {}

    def load(self, bl_collection: T.bpy_prop_collection, bl_collection_property: T.Property, visit_state: VisitState):

        if len(bl_collection) == 0:
            self._data.clear()
            return self

        if is_soable_collection(bl_collection_property):
            # TODO too much work at load time to find soable information. Do it once for all.

            # Hybrid array_of_struct/ struct_of_array
            # Hybrid because MeshVertex.groups does not have a fixed size and is not soa-able, but we want
            # to treat other MeshVertex members as SOAs.
            # Could be made more efficient later on. Keep the code simple until save() is implemented
            # and we need better
            prototype_item = bl_collection[0]
            item_bl_rna = bl_collection_property.fixed_type.bl_rna
            for attr_name, bl_rna_property in visit_state.context.properties(item_bl_rna):
                if is_soable_property(bl_rna_property):
                    # element type supported by foreach_get
                    self._data[attr_name] = SoaElement().load(bl_collection, attr_name, prototype_item)
                else:
                    # no foreach_get (variable length arrays like MeshVertex.groups, enums, ...)
                    self._data[attr_name] = AosElement().load(bl_collection, item_bl_rna, attr_name, visit_state)
        else:
            # no keys means it is a sequence. However bl_collection.items() returns [(index, item)...]
            is_sequence = not bl_collection.keys()
            if is_sequence:
                # easier for the encoder to always have a dict
                self._data = {MIXER_SEQUENCE: [BpyStructProxy().load(v, visit_state) for v in bl_collection.values()]}
            else:
                self._data = {k: BpyStructProxy().load(v, visit_state) for k, v in bl_collection.items()}

        return self

    def save(self, bl_instance: any, attr_name: str):
        """
        Save this proxy into a Blender object
        """
        target = getattr(bl_instance, attr_name)
        sequence = self._data.get(MIXER_SEQUENCE)
        if sequence:
            srna = bl_instance.bl_rna.properties[attr_name].srna
            if srna:
                if srna.bl_rna is bpy.types.CurveMapPoints.bl_rna:
                    write_curvemappoints(target, sequence)
                elif srna.bl_rna is bpy.types.MetaBallElements.bl_rna:
                    write_metaballelements(target, sequence)

            elif len(target) == len(sequence):
                for i, v in enumerate(sequence):
                    # TODO this way can only save items at pre-existing slots. The bpy_prop_collection API
                    # uses struct specific API and ctors:
                    # - CurveMapPoints uses: .new(x, y) and .remove(point), no .clear(). new() inserts in head !
                    #   Must have at least 2 points left !
                    # - NodeTreeOutputs uses: .new(type, name), .remove(socket), has .clear()
                    # - ActionFCurves uses: .new(data_path, index=0, action_group=""), .remove(fcurve)
                    # - GPencilStrokePoints: .add(count), .pop()
                    write_attribute(target, i, v)
            else:
                logging.warning(
                    f"Not implemented: write sequence of different length (incoming: {len(sequence)}, existing: {len(target)})for {bl_instance}.{attr_name}"
                )
        else:
            # dictionary
            for k, v in self._data.items():
                write_attribute(target, k, v)


# TODO derive from BpyIDProxy
class BpyPropDataCollectionProxy(Proxy):
    """
    Proxy to a bpy_prop_collection of ID in bpy.data. May not work as is for bpy_prop_collection on non-ID
    """

    def __init__(self):
        self._data: Mapping[str, BpyIDProxy] = {}

    def __len__(self):
        return len(self._data)

    def load_as_ID(self, bl_collection: bpy.types.bpy_prop_collection, visit_state: VisitState):  # noqa N802
        """
        Load bl_collection elements as plain IDs, with all element properties. Use this to load from bpy.data
        """
        for name, item in bl_collection.items():
            collection_name = BlendData.instance().bl_collection_name_from_ID(item)
            with visit_state.debug_context.enter(name, item):
                ensure_uuid(item)
                self._data[name] = BpyIDProxy().load(item, visit_state, (collection_name, name))

        return self

    def load_as_IDref(self, bl_collection: bpy.types.bpy_prop_collection, visit_state: VisitState):  # noqa N802
        """
        Load bl_collection elements as referenced into bpy.data
        """
        for name, item in bl_collection.items():
            with visit_state.debug_context.enter(name, item):
                self._data[name] = BpyIDRefProxy().load(item, visit_state)
        return self

    def save(self, bl_instance: any, attr_name: str):
        """
        Load a Blender object into this proxy
        """
        target = getattr(bl_instance, attr_name)
        for k, v in self._data.items():
            write_attribute(target, k, v)

    def find(self, key: str):
        return self._data.get(key)

    def update(self, diff, visit_state: VisitState) -> List[BpyIDProxy]:
        """
        Update the proxy according to the diff
        """
        created = []
        blenddata = BlendData.instance()
        for name, collection_name in diff.items_added.items():
            # warning this is the killer access in linear time
            id_ = blenddata.collection(collection_name)[name]
            uuid = ensure_uuid(id_)
            blenddata_path = (collection_name, name)
            visit_state.root_ids.add(id_)
            visit_state.ids[uuid] = id_
            proxy = BpyIDProxy().load(id_, visit_state, blenddata_path)
            visit_state.id_proxies[uuid] = proxy
            self._data[name] = proxy
            created.append(proxy)

        for name, uuid in diff.items_removed:
            del self._data[name]
            id_ = visit_state.ids[uuid]
            visit_state.root_ids.remove(id_)
            del visit_state.id_proxies[uuid]
            del visit_state.ids[uuid]

        for old_name, new_name in diff.items_renamed:
            self._data[new_name] = self._data[old_name]
            del self._data[old_name]

        return created


# to sort delta in the bottom up order in the hierarchy ( creation order, mesh before object, ..)
_creation_order = {"scenes": 20, "objects": 10}


def _pred_by_creation_order(item: Tuple[str, Any]):
    return _creation_order.get(item[0], 0)


class BpyBlendProxy(Proxy):
    def __init__(self, *args, **kwargs):
        # ID elements stored in bpy.data.* collections, computed before recursive visit starts:
        self.root_ids: RootIds = set()
        self.id_proxies: IDProxies = {}
        # Only needed to cleanup root_ids and id_proxies on ID removal
        self.ids: IDs = {}
        self._data: Mapping[str, BpyPropDataCollectionProxy] = {
            name: BpyPropDataCollectionProxy() for name in BlendData.instance().collection_names()
        }

    def get_non_empty_collections(self):
        return {key: value for key, value in self._data.items() if len(value) > 0}

    def load(self, context: Context):
        for name, _ in context.properties(bpy_type=T.BlendData):
            if name in collection_name_to_type:
                # TODO use BlendData
                bl_collection = getattr(bpy.data, name)
                for _id_name, item in bl_collection.items():
                    uuid = ensure_uuid(item)
                    self.root_ids.add(item)
                    self.ids[uuid] = item

        visit_state = VisitState(self.root_ids, self.id_proxies, self.ids, context)

        for name, _ in context.properties(bpy_type=T.BlendData):
            collection = getattr(bpy.data, name)
            with visit_state.debug_context.enter(name, collection):
                self._data[name] = BpyPropDataCollectionProxy().load_as_ID(collection, visit_state)
        return self

    def find(self, collection_name: str, key: str) -> BpyIDProxy:
        if not self._data:
            return None
        collection_proxy = self._data.get(collection_name)
        if collection_proxy is None:
            return None
        return collection_proxy.find(key)

    def update(self, diff: BpyBlendDiff, context: Context, depsgraph_updates: T.bpy_prop_collection):
        """
        Update the proxy using the state of the Blendata collections (ID creation, deletion)
        and the depsgraph updates (ID modification)
        """
        updated_proxies = []

        # Update the bpy.data collections status and get the list of newly created bpy.data entries.
        # Updated proxies will contain the IDs to send as an initial transfer
        visit_state = VisitState(self.root_ids, self.id_proxies, self.ids, context)
        deltas = sorted(diff.collection_deltas, key=_pred_by_creation_order)
        for delta_name, delta in deltas:
            created = self._data[delta_name].update(delta, visit_state)
            updated_proxies.extend(created)

        # Update the ID proxies from the depsgraph update
        # this should iterate inside_out (Object.data, Object) in the adequate creation order
        # (creating an Object requires its data)
        updates = reversed([update.id.original for update in depsgraph_updates])
        # WARNING:
        #   depsgraph_updates[i].id.original IS NOT bpy.lights['Point']
        # or whatever as you might expect, so you cannot use it to index into the map
        # to find the proxy to update.
        # However
        #   - mixer_uuid attributes have the same value
        #   - __hash__() returns the same value
        for id_ in updates:
            if not any((isinstance(id_, t) for t in safe_depsgraph_updates)):
                continue
            logger.info("Updating %s(%s)")
            proxy = self.id_proxies.get(id_.mixer_uuid)
            if proxy is None:
                logger.warning("BpyBlendProxy.update(): Ignoring %s (no proxy)", id_)
                continue
            proxy.load(id_, visit_state)
            updated_proxies.append(proxy)

        return updated_proxies

    def clear(self):
        self._data.clear()
        self.root_ids.clear()
        self.id_proxies.clear()
        self.ids.clear()

    def debug_check_id_proxies(self):
        return 0
        # try to find stale entries ASAP: access them all
        dummy = 0
        try:
            dummy = sum(len(id_.name) for id_ in self.root_ids)
        except ReferenceError:
            logger.warning("BpyBlendProxy: Stale reference in root_ids")
        try:
            dummy = sum(len(id_.name) for id_ in self.ids.values())
        except ReferenceError:
            logger.warning("BpyBlendProxy: Stale reference in root_ids")

        return dummy


proxy_classes = [
    BpyIDProxy,
    BpyIDRefProxy,
    BpyStructProxy,
    BpyPropertyGroupProxy,
    BpyPropStructCollectionProxy,
    BpyPropDataCollectionProxy,
    SoaElement,
    AosElement,
]


def write_attribute(bl_instance, key: Union[str, int], value: Any):
    """
    Load a property into a python object of the appropriate type, be it a Proxy or a native python object
    """
    type_ = type(value)
    if bl_instance is None:
        logging.warning(f"unexpected write None attribute")
        return

    if type_ not in proxy_classes:
        # TEMP we should not have readonly items
        assert type(key) is str
        if not bl_instance.bl_rna.properties[key].is_readonly:
            try:
                setattr(bl_instance, key, value)
            except Exception as e:
                logging.warning(f"write attribute skipped {key} for {bl_instance}...")
                logging.warning(f" ...Error: {repr(e)}")
    else:
        value.save(bl_instance, key)

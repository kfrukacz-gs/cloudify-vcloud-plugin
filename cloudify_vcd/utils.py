from copy import deepcopy

from pyvcloud.vcd.utils import task_to_dict
from lxml.objectify import StringElement, IntElement, ObjectifiedElement, BoolElement
from pyvcloud.vcd.exceptions import (
    VcdTaskException,
    EntityNotFoundException,
)

from cloudify import ctx
from cloudify.exceptions import NonRecoverableError
from cloudify.constants import NODE_INSTANCE, RELATIONSHIP_INSTANCE

from .constants import CLIENT_CONFIG_KEYS, CLIENT_CREDENTIALS_KEYS, TYPE_MATRIX, NO_RESOURCE_OK
from vcd_plugin_sdk.connection import VCloudConnect


class ResourceData(object):

    def __init__(self,
                 context,
                 external,
                 resource_id,
                 client_config,
                 vdc,
                 resource_config,
                 resource_class):
        self._resources = []
        self.add(
            context,
            external,
            resource_id,
            client_config,
            vdc,
            resource_config,
            resource_class)

    @property
    def primary(self):
        return self._return_resource_args(0)

    @property
    def secondary(self):
        if len(self._resources) == 2:
            return self._return_resource_args(1)
        return

    @property
    def primary_id(self):
        return self._resources[0].get('id')

    @property
    def primary_class(self):
        return self._resources[0].get('class')

    @property
    def primary_client(self):
        return self._resources[0].get('client')

    @property
    def primary_ctx(self):
        return self._resources[0].get('ctx')

    @property
    def primary_external(self):
        return self._resources[0].get('external')

    @property
    def primary_resource(self):
        return self.primary_class(self.primary_id,
                                  connection=self.primary_client)

    def add(self,
            context,
            external,
            resource_id,
            client_config,
            vdc,
            resource_config,
            resource_class):
        self._resources.append(
            {'external': external,
             'id': resource_id,
             'client': client_config,
             'vdc': vdc,
             'config': resource_config,
             'ctx': context,
             'class': resource_class})

    def _return_resource_args(self, index):
        return [self._resources[index].get('external'),
                self._resources[index].get('id'),
                self._resources[index].get('client'),
                self._resources[index].get('vdc'),
                self._resources[index].get('config'),
                self._resources[index].get('class'),
                self._resources[index].get('ctx')]


def is_relationship(_ctx=None):
    _ctx = _ctx or ctx
    return _ctx.type == RELATIONSHIP_INSTANCE


def is_node_instance(_ctx=None):
    _ctx = _ctx or ctx
    return _ctx.type == NODE_INSTANCE


def get_resource_config(node, instance):
    return instance.get('resource_config', node.get('resource_config'))


def is_external_resource(node, instance):
    external_node = node.get('use_external_resource', False)
    bad_request_retry = instance.get(
        '__RETRY_BAD_REQUEST', False)
    if external_node:
        return external_node
    elif not bad_request_retry and ctx.operation.retry_number:
        return True
    return False


def get_resource_id(node, instance):
    return instance.get('resource_id', node.get('resource_id'))


def get_client_config(node):
    client_config = node.get('client_config', {})
    vdc = client_config.get('vdc')

    def _get_config():
        d = {}
        for key in CLIENT_CONFIG_KEYS:
            d[key] = client_config.get(key)
        d.update(client_config.get('configuration_kwargs'))
        return d

    def _get_creds():
        d = {}
        for key in CLIENT_CREDENTIALS_KEYS:
            d[key] = client_config.get(key)
        d.update(client_config.get('credentials_kwargs'))
        return d

    return VCloudConnect(ctx.logger, _get_config(), _get_creds()), vdc


def get_ctxs(_ctx):
    """
    Get the current context(s).

    :param param:
    :return: Either the ctx, or the ctx.source and ctx.target
    """

    _ctx = _ctx or ctx

    if is_relationship(_ctx):
        return _ctx.source, _ctx.target
    elif is_node_instance(_ctx):
        return _ctx, None
    else:
        raise Exception('Bad ctx type: {bad_type}.'.format(bad_type=_ctx.type))


def get_resource_class(type_hierarchy):
    for hierarchy_item in type_hierarchy:
        if hierarchy_item in TYPE_MATRIX:
            return TYPE_MATRIX.get(hierarchy_item)
    raise NonRecoverableError(
        'A resource type matching node hierarchy {h} not found. '
        'Use one of {t}, or derive type from those types.'.format(
            h=type_hierarchy, t=TYPE_MATRIX.keys()))


def get_resource_data(__ctx):
    """Initialize the ctx, resource id, client config, vdc, resource config
    for the node instance resource or both relationship resources.
    Primary = Node Template Node Instance or Relationship Source
    Secondary = Relationship Target if applicable.

    :param __ctx: ctx from operation
    :return: list of tuple where tuple contains a resource ctx, ID,
    client config, VDC string and resource config.
    """
    primary, secondary = get_ctxs(__ctx)
    primary_resource_id = get_resource_id(
        primary.node.properties, primary.instance.runtime_properties)
    primary_external = is_external_resource(
        primary.node.properties,
        primary.instance.runtime_properties)
    primary_client_config, primary_vdc = get_client_config(
        primary.node.properties)
    primary_resource_config = get_resource_config(
        primary.node.properties, primary.instance.runtime_properties)
    classes = get_resource_class(primary.node.type_hierarchy)

    base_properties = ResourceData(
        primary,
        primary_external,
        primary_resource_id,
        primary_client_config,
        primary_vdc,
        primary_resource_config,
        classes[0])
    if secondary:
        secondary_resource_id = get_resource_id(
            secondary.node.properties, secondary.instance.runtime_properties)
        secondary_external = is_external_resource(
            secondary.node.properties,
            secondary.instance.runtime_properties)
        secondary_client_config, secondary_vdc = get_client_config(
            secondary.node.properties)
        secondary_resource_config = get_resource_config(
            secondary.node.properties, secondary.instance.runtime_properties)
        if len(classes) == 1:
            secondary_class = None
        else:
            secondary_class = classes[1]
        base_properties.add(
            secondary,
            secondary_external,
            secondary_resource_id,
            secondary_client_config,
            secondary_vdc,
            secondary_resource_config,
            secondary_class)
    return base_properties


def update_runtime_properties(current_ctx, props):
    props = cleanup_objectify(props)
    ctx.logger.debug('Updating instance with properties {props}.'.format(
        props=props))
    if is_relationship():
        if current_ctx.instance.id == ctx.source.instance.id:
            ctx.source.instance.runtime_properties.update(props)
            ctx.source.instance.runtime_properties.dirty = True
            ctx.source.instance.update()
        elif current_ctx.instance.id == ctx.target.instance.id:
            ctx.target.instance.runtime_properties.update(props)
            ctx.target.instance.runtime_properties.dirty = True
            ctx.target.instance.update()
        else:
            ctx.logger.error(
                'Error updating instance {_id} props {props}.'.format(
                    _id=current_ctx.instance.id, props=props))
    elif is_node_instance():
        ctx.instance.runtime_properties.update(props)
        ctx.instance.runtime_properties.dirty = True
        ctx.instance.update()


def cleanup_runtime_properties(current_ctx):
    ctx.logger.debug('Cleaning instance {_id} props.'.format(
        _id=current_ctx.instance.id))
    if is_relationship():
        if current_ctx.instance.id == ctx.source.instance.id:
            for key in list(ctx.source.instance.runtime_properties.keys()):
                del ctx.source.instance.runtime_properties[key]
            ctx.source.instance.runtime_properties.dirty = True
            ctx.source.instance.update()
        elif current_ctx.instance.id == ctx.target.instance.id:
            for key in list(ctx.target.instance.runtime_properties.keys()):
                del ctx.target.instance.runtime_properties[key]
            ctx.target.instance.runtime_properties.dirty = True
            ctx.target.instance.update()
        else:
            ctx.logger.error(
                'Error deleting instance {_id} props.'.format(
                    _id=current_ctx.instance.id))
    elif is_node_instance():
        for key in list(ctx.instance.runtime_properties.keys()):
            del ctx.instance.runtime_properties[key]
        ctx.instance.runtime_properties.dirty = True
        ctx.instance.update()


def cleanup_objectify(data):
    data = deepcopy(data)
    if isinstance(data, tuple):
        if len(data) == 2:
            data = {str(data[0]): data[1]}
        else:
            data = list(data)
    if isinstance(data, dict):
        for k, v in list(data.items()):
            del data[k]
            data[str(k)] = cleanup_objectify(v)
    elif isinstance(data, list):
        for n in range(0, len(data)):
            data[n] = cleanup_objectify(data[n])
    elif isinstance(data, (BoolElement, StringElement, IntElement, ObjectifiedElement)):
        data = data.text
    return data


def find_rels_by_type(node_instance, rel_type):
    '''
        Finds all specified relationships of the Cloudify
        instance.
    :param `cloudify.context.NodeInstanceContext` node_instance:
        Cloudify node instance.
    :param str rel_type: Cloudify relationship type to search
        node_instance.relationships for.
    :returns: List of Cloudify relationships
    '''
    return [x for x in node_instance.relationships
            if rel_type in x.type_hierarchy]


def find_rel_by_type(node_instance, rel_type):
    rels = find_rels_by_type(node_instance, rel_type)
    if len(rels) == 1:
        return rels[0]
    return


def find_resource_id_from_relationship_by_type(node_instance, rel_type):
    rel = find_rel_by_type(node_instance, rel_type)
    return rel.target.instance.runtime_properties.get('resource_id')


def use_external_resource(external,
                          resource,
                          override,
                          resource_type,
                          resource_name):

    if not external:
        ctx.logger.debug(
            'The {t} {r} is not external. '
            'Proceeding with operation.'.format(
                t=resource_type, r=resource_name))
        return False

    elif external and not resource and override:
        ctx.logger.debug(
            'The {t} {r} is external, but does not exist, '
            'proceeding with operation, because override is True.'.format(
                t=resource_type, r=resource_name))
        return False

    elif external and resource:
        ctx.logger.debug(
            'The {t} {r} is external, and does exist, '
            'not proceeding with operation.'.format(
                t=resource_type, r=resource_name))
        return True

    else:
        raise NonRecoverableError(
            'The {r} {r} is external, '
            'but does not exist and override is False.'.format(
                t=resource_type, r=resource_name))


def expose_props(operation_name, resource=None, new_props=None, _ctx=None):
    _ctx = _ctx or ctx
    new_props = new_props or {}

    if 'create' in operation_name:
        new_props.update({'__created': True})
    elif 'delete' in operation_name:
        new_props.update({'__deleted': True})
        cleanup_runtime_properties(ctx)

    if operation_name not in NO_RESOURCE_OK:
        try:
            new_props.update({
                'resource_id': resource.name,
                'data': resource.exposed_data,
                'tasks': resource.tasks,
            })
        except EntityNotFoundException:
            raise NonRecoverableError(
                'The resource {n} was not found.'.format(n=resource.name))

    new_props.update({'__RETRY_BAD_REQUEST': False})
    update_runtime_properties(_ctx, new_props)


def get_last_task(task):
    try:
        return task_to_dict(task.Tasks.Task)
    except AttributeError:
        return task


def vcd_busy_exception(exc):
    if 'is busy, cannot proceed with the operation' in str(exc):
        return True
    return False


def vcd_unclear_exception(exc):
    if 'Status code: 400/None, None' in str(exc):
        return True
    return False


def cannot_deploy(exc):
    if 'Cannot deploy organization VDC network' in str(exc):
        return True
    return False


def check_if_task_successful(_resource, task):
    if task:
        try:
            return _resource.task_successful(task)
        except VcdTaskException as e:
            if cannot_deploy(e):
                raise NonRecoverableError(str(e))
            ctx.logger.error(
                'Unhandled state validation error: {e}.'.format(e=str(e)))
            return False
    return True

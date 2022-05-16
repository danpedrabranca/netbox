from collections import defaultdict

from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Sum
from django.urls import reverse

from dcim.choices import *
from dcim.constants import *
from dcim.fields import PathField
from dcim.utils import decompile_path_node, flatten_path, object_to_path_node, path_node_to_object
from netbox.models import NetBoxModel
from utilities.fields import ColorField
from utilities.querysets import RestrictedQuerySet
from utilities.utils import to_meters
from wireless.models import WirelessLink
from .devices import Device
from .device_components import FrontPort, RearPort


__all__ = (
    'Cable',
    'CablePath',
    'CableTermination',
)


#
# Cables
#

class Cable(NetBoxModel):
    """
    A physical connection between two endpoints.
    """
    type = models.CharField(
        max_length=50,
        choices=CableTypeChoices,
        blank=True
    )
    status = models.CharField(
        max_length=50,
        choices=LinkStatusChoices,
        default=LinkStatusChoices.STATUS_CONNECTED
    )
    tenant = models.ForeignKey(
        to='tenancy.Tenant',
        on_delete=models.PROTECT,
        related_name='cables',
        blank=True,
        null=True
    )
    label = models.CharField(
        max_length=100,
        blank=True
    )
    color = ColorField(
        blank=True
    )
    length = models.DecimalField(
        max_digits=8,
        decimal_places=2,
        blank=True,
        null=True
    )
    length_unit = models.CharField(
        max_length=50,
        choices=CableLengthUnitChoices,
        blank=True,
    )
    # Stores the normalized length (in meters) for database ordering
    _abs_length = models.DecimalField(
        max_digits=10,
        decimal_places=4,
        blank=True,
        null=True
    )
    # Cache the associated device (where applicable) for the A and B terminations. This enables filtering of Cables by
    # their associated Devices.
    _termination_a_device = models.ForeignKey(
        to=Device,
        on_delete=models.CASCADE,
        related_name='+',
        blank=True,
        null=True
    )
    _termination_b_device = models.ForeignKey(
        to=Device,
        on_delete=models.CASCADE,
        related_name='+',
        blank=True,
        null=True
    )

    class Meta:
        ordering = ('pk',)

    def __init__(self, *args, a_terminations=None, b_terminations=None, **kwargs):
        super().__init__(*args, **kwargs)

        # A copy of the PK to be used by __str__ in case the object is deleted
        self._pk = self.pk

        # Cache the original status so we can check later if it's been changed
        self._orig_status = self.status

        # Assign associated CableTerminations (if any)
        terminations = []
        if a_terminations and type(a_terminations) is list:
            terminations.extend([
                CableTermination(cable=self, cable_end='A', termination=t) for t in a_terminations
            ])
        if b_terminations and type(b_terminations) is list:
            terminations.extend([
                CableTermination(cable=self, cable_end='B', termination=t) for t in b_terminations
            ])
        if terminations:
            assert self.pk is None
            self._terminations = terminations
        else:
            self._terminations = []

    @classmethod
    def from_db(cls, db, field_names, values):
        """
        Cache the original A and B terminations of existing Cable instances for later reference inside clean().
        """
        instance = super().from_db(db, field_names, values)

        # instance._orig_termination_a_type_id = instance.termination_a_type_id
        # instance._orig_termination_a_ids = instance.termination_a_ids
        # instance._orig_termination_b_type_id = instance.termination_b_type_id
        # instance._orig_termination_b_ids = instance.termination_b_ids

        return instance

    def __str__(self):
        pk = self.pk or self._pk
        return self.label or f'#{pk}'

    def get_absolute_url(self):
        return reverse('dcim:cable', args=[self.pk])

    def clean(self):
        super().clean()

        # TODO: Is this validation still necessary?
        # # Check that two connected RearPorts have the same number of positions (if both are >1)
        # if isinstance(self.termination_a, RearPort) and isinstance(self.termination_b, RearPort):
        #     if self.termination_a.positions > 1 and self.termination_b.positions > 1:
        #         if self.termination_a.positions != self.termination_b.positions:
        #             raise ValidationError(
        #                 f"{self.termination_a} has {self.termination_a.positions} position(s) but "
        #                 f"{self.termination_b} has {self.termination_b.positions}. "
        #                 f"Both terminations must have the same number of positions (if greater than one)."
        #             )

        # Validate length and length_unit
        if self.length is not None and not self.length_unit:
            raise ValidationError("Must specify a unit when setting a cable length")
        elif self.length is None:
            self.length_unit = ''

    def save(self, *args, **kwargs):

        # Store the given length (if any) in meters for use in database ordering
        if self.length and self.length_unit:
            self._abs_length = to_meters(self.length, self.length_unit)
        else:
            self._abs_length = None

        # TODO: Move to CableTermination
        # # Store the parent Device for the A and B terminations (if applicable) to enable filtering
        # if hasattr(self.termination_a[0], 'device'):
        #     self._termination_a_device = self.termination_a[0].device
        # if hasattr(self.termination_b[0], 'device'):
        #     self._termination_b_device = self.termination_b[0].device

        super().save(*args, **kwargs)

        # Update the private pk used in __str__ in case this is a new object (i.e. just got its pk)
        self._pk = self.pk

    def get_status_color(self):
        return LinkStatusChoices.colors.get(self.status)

    def get_a_terminations(self):
        return [
            term.termination for term in CableTermination.objects.filter(cable=self, cable_end='A')
        ]

    def get_b_terminations(self):
        return [
            term.termination for term in CableTermination.objects.filter(cable=self, cable_end='B')
        ]


class CableTermination(models.Model):
    """
    A mapping between side A or B of a Cable and a terminating object (e.g. an Interface or CircuitTermination).
    """
    cable = models.ForeignKey(
        to='dcim.Cable',
        on_delete=models.CASCADE,
        related_name='terminations'
    )
    cable_end = models.CharField(
        max_length=1,
        choices=CableEndChoices,
        verbose_name='End'
    )
    termination_type = models.ForeignKey(
        to=ContentType,
        limit_choices_to=CABLE_TERMINATION_MODELS,
        on_delete=models.PROTECT,
        related_name='+'
    )
    termination_id = models.PositiveBigIntegerField()
    termination = GenericForeignKey(
        ct_field='termination_type',
        fk_field='termination_id'
    )

    objects = RestrictedQuerySet.as_manager()

    class Meta:
        ordering = ('cable', 'cable_end', 'pk')
        constraints = (
            models.UniqueConstraint(
                fields=('termination_type', 'termination_id'),
                name='unique_termination'
            ),
        )

    def __str__(self):
        return f'Cable {self.cable} to {self.termination}'

    def clean(self):
        super().clean()

        # Validate interface type (if applicable)
        if self.termination_type.model == 'interface' and self.termination.type in NONCONNECTABLE_IFACE_TYPES:
            raise ValidationError({
                'termination': f'Cables cannot be terminated to {self.termination.get_type_display()} interfaces'
            })

        # A CircuitTermination attached to a ProviderNetwork cannot have a Cable
        if self.termination_type.model == 'circuittermination' and self.termination.provider_network is not None:
            raise ValidationError({
                'termination': "Circuit terminations attached to a provider network may not be cabled."
            })

        # TODO
        # # A front port cannot be connected to its corresponding rear port
        # if (
        #     type_a in ['frontport', 'rearport'] and
        #     type_b in ['frontport', 'rearport'] and
        #     (
        #         getattr(self.termination_a, 'rear_port', None) == self.termination_b or
        #         getattr(self.termination_b, 'rear_port', None) == self.termination_a
        #     )
        # ):
        #     raise ValidationError("A front port cannot be connected to it corresponding rear port")

        # TODO
        # # Check that termination types are compatible
        # if type_b not in COMPATIBLE_TERMINATION_TYPES.get(type_a):
        #     raise ValidationError(
        #         f"Incompatible termination types: {self.termination_a_type} and {self.termination_b_type}"
        #     )


class CablePath(models.Model):
    """
    A CablePath instance represents the physical path from an origin to a destination, including all intermediate
    elements in the path. Every instance must specify an `origin`, whereas `destination` may be null (for paths which do
    not terminate on a PathEndpoint).

    `path` contains a list of nodes within the path, each represented by a tuple of (type, ID). The first element in the
    path must be a Cable instance, followed by a pair of pass-through ports. For example, consider the following
    topology:

                     1                              2                              3
        Interface A --- Front Port A | Rear Port A --- Rear Port B | Front Port B --- Interface B

    This path would be expressed as:

    CablePath(
        origin = Interface A
        destination = Interface B
        path = [Cable 1, Front Port A, Rear Port A, Cable 2, Rear Port B, Front Port B, Cable 3]
    )

    `is_active` is set to True only if 1) `destination` is not null, and 2) every Cable within the path has a status of
    "connected".
    """
    path = models.JSONField(
        default=list
    )
    is_active = models.BooleanField(
        default=False
    )
    is_complete = models.BooleanField(
        default=False
    )
    is_split = models.BooleanField(
        default=False
    )
    _nodes = PathField()

    class Meta:
        pass

    def __str__(self):
        status = ' (active)' if self.is_active else ' (split)' if self.is_split else ''
        return f"Path #{self.pk}: {len(self.path)} nodes{status}"

    def save(self, *args, **kwargs):

        # Save the flattened nodes list
        self._nodes = flatten_path(self.path)

        super().save(*args, **kwargs)

        # Record a direct reference to this CablePath on its originating object(s)
        origin_model = self.origins[0]._meta.model
        origin_ids = [o.id for o in self.origins]
        origin_model.objects.filter(pk__in=origin_ids).update(_path=self.pk)

    @property
    def origin_type(self):
        ct_id, _ = decompile_path_node(self.path[0][0])
        return ContentType.objects.get_for_id(ct_id)

    @property
    def destination_type(self):
        if not self.is_complete:
            return None
        ct_id, _ = decompile_path_node(self.path[-1][0])
        return ContentType.objects.get_for_id(ct_id)

    @property
    def path_objects(self):
        """
        Cache and return the complete path as lists of objects, derived from their annotation within the path.
        """
        if not hasattr(self, '_path_objects'):
            self._path_objects = self._get_path()
        return self._path_objects

    @property
    def origins(self):
        """
        Return the list of originating objects (from cache, if available).
        """
        if hasattr(self, '_path_objects'):
            return self.path_objects[0]
        return [
            path_node_to_object(node) for node in self.path[0]
        ]

    @property
    def destinations(self):
        """
        Return the list of destination objects (from cache, if available), if the path is complete.
        """
        if not self.is_complete:
            return []
        if hasattr(self, '_path_objects'):
            return self.path_objects[-1]
        return [
            path_node_to_object(node) for node in self.path[-1]
        ]

    @property
    def segment_count(self):
        return int(len(self.path) / 3)

    @classmethod
    def from_origin(cls, terminations):
        """
        Create a new CablePath instance as traced from the given termination objects. These can be any object to which a
        Cable or WirelessLink connects (interfaces, console ports, circuit termination, etc.). All terminations must be
        of the same type and must belong to the same parent object.
        """
        from circuits.models import CircuitTermination

        path = []
        position_stack = []
        is_complete = False
        is_active = True
        is_split = False

        while terminations:

            # Terminations must all be of the same type and belong to the same parent
            assert all(isinstance(t, type(terminations[0])) for t in terminations[1:])
            assert all(t.parent_object == terminations[0].parent_object for t in terminations[1:])

            # Step 1: Record the near-end termination object(s)
            path.append([
                object_to_path_node(t) for t in terminations
            ])

            # Step 2: Determine the attached link (Cable or WirelessLink), if any
            link = terminations[0].link
            assert all(t.link == link for t in terminations[1:])
            if link is None and len(path) == 1:
                # If this is the start of the path and no link exists, return None
                return None
            elif link is None:
                # Otherwise, halt the trace if no link exists
                break
            assert type(link) in (Cable, WirelessLink)

            # Step 3: Record the link and update path status if not "connected"
            path.append([object_to_path_node(link)])
            if hasattr(link, 'status') and link.status != LinkStatusChoices.STATUS_CONNECTED:
                is_active = False

            # Step 4: Determine the far-end terminations
            if isinstance(link, Cable):
                termination_type = ContentType.objects.get_for_model(terminations[0])
                local_cable_terminations = CableTermination.objects.filter(
                    termination_type=termination_type,
                    termination_id__in=[t.pk for t in terminations]
                )
                # Terminations must all belong to same end of Cable
                local_cable_end = local_cable_terminations[0].cable_end
                assert all(ct.cable_end == local_cable_end for ct in local_cable_terminations[1:])
                remote_cable_terminations = CableTermination.objects.filter(
                    cable=link,
                    cable_end='A' if local_cable_end == 'B' else 'B'
                )
                remote_terminations = [ct.termination for ct in remote_cable_terminations]
            else:
                # WirelessLink
                remote_terminations = [link.interface_b] if link.interface_a is terminations[0] else [link.interface_a]

            # Step 5: Record the far-end termination object(s)
            path.append([
                object_to_path_node(t) for t in remote_terminations
            ])

            # Step 6: Determine the "next hop" terminations, if applicable
            if isinstance(remote_terminations[0], FrontPort):
                # Follow FrontPorts to their corresponding RearPorts
                rear_ports = RearPort.objects.filter(
                    pk__in=[t.rear_port_id for t in remote_terminations]
                )
                if len(rear_ports) > 1:
                    assert all(rp.positions == 1 for rp in rear_ports)
                elif rear_ports[0].positions > 1:
                    position_stack.append([fp.rear_port_position for fp in remote_terminations])

                terminations = rear_ports

            elif isinstance(remote_terminations[0], RearPort):

                if len(remote_terminations) > 1 or remote_terminations[0].positions == 1:
                    front_ports = FrontPort.objects.filter(
                        rear_port_id__in=[rp.pk for rp in remote_terminations],
                        rear_port_position=1
                    )
                elif position_stack:
                    front_ports = FrontPort.objects.filter(
                        rear_port_id=remote_terminations[0].pk,
                        rear_port_position__in=position_stack.pop()
                    )
                else:
                    # No position indicated: path has split, so we stop at the RearPorts
                    is_split = True
                    break

                terminations = front_ports

            elif isinstance(remote_terminations[0], CircuitTermination):
                # Follow a CircuitTermination to its corresponding CircuitTermination (A to Z or vice versa)
                term_side = remote_terminations[0].term_side
                assert all(ct.term_side == term_side for ct in remote_terminations[1:])
                circuit_termination = CircuitTermination.objects.filter(
                    circuit=remote_terminations[0].circuit,
                    term_side='Z' if term_side == 'A' else 'A'
                ).first()
                if circuit_termination is None:
                    break
                elif circuit_termination.provider_network:
                    # Circuit terminates to a ProviderNetwork
                    path.extend([
                        [object_to_path_node(circuit_termination)],
                        [object_to_path_node(circuit_termination.provider_network)],
                    ])
                    break
                elif circuit_termination.site and not circuit_termination.cable:
                    # Circuit terminates to a Site
                    path.extend([
                        [object_to_path_node(circuit_termination)],
                        [object_to_path_node(circuit_termination.site)],
                    ])
                    break

                terminations = [circuit_termination]

            # Anything else marks the end of the path
            else:
                is_complete = True
                break

        return cls(
            path=path,
            is_complete=is_complete,
            is_active=is_active,
            is_split=is_split
        )

    def retrace(self):
        """
        Retrace the path from the currently-defined originating termination(s)
        """
        _new = self.from_origin(self.origins)
        if _new:
            self.path = _new.path
            self.is_complete = _new.is_complete
            self.is_active = _new.is_active
            self.is_split = _new.is_split
            self.save()
        else:
            self.delete()

    def _get_path(self):
        """
        Return the path as a list of prefetched objects.
        """
        # Compile a list of IDs to prefetch for each type of model in the path
        to_prefetch = defaultdict(list)
        for node in self._nodes:
            ct_id, object_id = decompile_path_node(node)
            to_prefetch[ct_id].append(object_id)

        # Prefetch path objects using one query per model type. Prefetch related devices where appropriate.
        prefetched = {}
        for ct_id, object_ids in to_prefetch.items():
            model_class = ContentType.objects.get_for_id(ct_id).model_class()
            queryset = model_class.objects.filter(pk__in=object_ids)
            if hasattr(model_class, 'device'):
                queryset = queryset.prefetch_related('device')
            prefetched[ct_id] = {
                obj.id: obj for obj in queryset
            }

        # Replicate the path using the prefetched objects.
        path = []
        for step in self.path:
            nodes = []
            for node in step:
                ct_id, object_id = decompile_path_node(node)
                nodes.append(prefetched[ct_id][object_id])
            path.append(nodes)

        return path

    def get_cable_ids(self):
        """
        Return all Cable IDs within the path.
        """
        cable_ct = ContentType.objects.get_for_model(Cable).pk
        cable_ids = []

        for node in self._nodes:
            ct, id = decompile_path_node(node)
            if ct == cable_ct:
                cable_ids.append(id)

        return cable_ids

    def get_total_length(self):
        """
        Return a tuple containing the sum of the length of each cable in the path
        and a flag indicating whether the length is definitive.
        """
        cable_ids = self.get_cable_ids()
        cables = Cable.objects.filter(id__in=cable_ids, _abs_length__isnull=False)
        total_length = cables.aggregate(total=Sum('_abs_length'))['total']
        is_definitive = len(cables) == len(cable_ids)

        return total_length, is_definitive

    def get_split_nodes(self):
        """
        Return all available next segments in a split cable path.
        """
        rearport = path_node_to_object(self._nodes[-1])

        return FrontPort.objects.filter(rear_port=rearport)

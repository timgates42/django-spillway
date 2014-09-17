from django.contrib.gis.db import models
from rest_framework import serializers, pagination

from spillway.collections import Feature, FeatureCollection, LinkedCRS
from spillway.fields import GeometryField, NDArrayField


class GeoModelSerializerOptions(serializers.ModelSerializerOptions):
    def __init__(self, meta):
        super(GeoModelSerializerOptions, self).__init__(meta)
        self.geom_field = getattr(meta, 'geom_field', None)


class GeoModelSerializer(serializers.ModelSerializer):
    """Serializer class for GeoModels."""
    _options_class = GeoModelSerializerOptions
    field_mapping = dict({models.GeometryField: GeometryField},
                         **serializers.ModelSerializer.field_mapping)

    def get_default_fields(self):
        """Returns a fields dict for this serializer with a 'geometry' field
        added.
        """
        fields = super(GeoModelSerializer, self).get_default_fields()
        # Set the geometry field name when it's undeclared.
        if not self.opts.geom_field:
            for name, field in fields.items():
                if isinstance(field, GeometryField):
                    self.opts.geom_field = name
        return fields


class FeatureSerializer(GeoModelSerializer):
    def __init__(self, *args, **kwargs):
        super(FeatureSerializer, self).__init__(*args, **kwargs)
        self.fields[self.opts.geom_field].set_default_source()

    @property
    def data(self):
        if self._data is None:
            data = super(FeatureSerializer, self).data
            if self.many or isinstance(data, (list, tuple)):
                try:
                    extent = self.object.extent()
                except AttributeError:
                    extent = ()
                    srid = None
                else:
                    srid = self.object.query._geo_field().srid
                self._data = FeatureCollection(features=data, crs=srid)
            else:
                fieldname = self.opts.geom_field
                try:
                    geom = getattr(self.object, fieldname)
                    extent = geom.extent
                except AttributeError:
                    extent = ()
                else:
                    self._data['crs'] = LinkedCRS(geom.srid)
        return self._data

    def to_native(self, obj):
        native = super(FeatureSerializer, self).to_native(obj)
        geometry = native.pop(self.opts.geom_field)
        pk = native.pop(obj._meta.pk.name, None)
        return Feature(pk, geometry, native)

    def from_native(self, obj, files=None):
        data = {self.opts.geom_field: obj.get('geometry')}
        data.update(obj.get('properties'))
        return super(FeatureSerializer, self).from_native(data, files)


class PaginatedFeatureSerializer(pagination.PaginationSerializer):
    results_field = 'features'


class RasterModelSerializerOptions(GeoModelSerializerOptions):
    def __init__(self, meta):
        super(RasterModelSerializerOptions, self).__init__(meta)
        self.raster_field = getattr(meta, 'raster_field', None)


class RasterModelSerializer(GeoModelSerializer):
    _options_class = RasterModelSerializerOptions

    def get_default_fields(self):
        fields = super(RasterModelSerializer, self).get_default_fields()
        if not self.opts.raster_field:
            for name, field in fields.items():
                if isinstance(field, serializers.FileField):
                    self.opts.raster_field = name
        request = self.context.get('request')
        render_format = request.accepted_renderer.format if request else None
        # Serialize image data as arrays when json is requested.
        if render_format == 'json':
            fields[self.opts.raster_field] = NDArrayField()
        elif render_format in ('api', 'html'):
            pass
        elif self.opts.raster_field and 'path' not in fields:
            # Add a filepath field for GDAL based renderers.
            fields['path'] = serializers.CharField(
                source='%s.path' % self.opts.raster_field)
        return fields

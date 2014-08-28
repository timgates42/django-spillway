import os
import tempfile
from wsgiref.util import FileWrapper
import zipfile

from django.contrib.gis.shortcuts import compress_kml
from django.conf import settings
from django.template import loader, Context
from rest_framework.pagination import PaginationSerializer
from rest_framework.renderers import BaseRenderer
from greenwich.geometry import Geometry
from greenwich.io import MemFileIO
from greenwich.raster import Raster, driver_for_path
import mapnik

from spillway.collections import FeatureCollection


class BaseGeoRenderer(BaseRenderer):
    """Base renderer for geographic features."""

    def _collection(self, data, renderer_context=None):
        pageinfo = {}
        results_field = self._results_field(renderer_context)
        results = data
        if data and isinstance(data, dict):
            if results_field in data:
                results = data.pop(results_field)
                pageinfo = data
            else:
                results = [data]
        return FeatureCollection(features=results, **pageinfo)

    def _results_field(self, context):
        """Returns the view's pagination serializer results field or the
        default value.
        """
        try:
            view = context.get('view')
            return view.pagination_serializer_class.results_field
        except AttributeError:
            return PaginationSerializer.results_field


class GeoJSONRenderer(BaseGeoRenderer):
    """Renderer which serializes to GeoJSON.

    This renderer purposefully avoids reserialization of GeoJSON from the
    spatial backend which greatly improves performance.
    """
    media_type = 'application/geojson'
    format = 'geojson'

    def render(self, data, accepted_media_type=None, renderer_context=None):
        """Returns *data* encoded as GeoJSON."""
        return str(self._collection(data, renderer_context))


class TemplateRenderer(BaseGeoRenderer):
    """Template based feature renderer."""
    template_name = None

    def render(self, data, accepted_media_type=None, renderer_context=None):
        collection = self._collection(data, renderer_context)
        template = loader.get_template(self.template_name)
        return template.render(Context({'features': collection['features']}))


class KMLRenderer(TemplateRenderer):
    """Renderer which serializes to KML."""
    media_type = 'application/vnd.google-earth.kml+xml'
    format = 'kml'
    template_name = 'spillway/placemarks.kml'


class KMZRenderer(KMLRenderer):
    """Renderer which serializes to KMZ."""
    media_type = 'application/vnd.google-earth.kmz'
    format = 'kmz'

    def render(self, *args, **kwargs):
        kmldata = super(KMZRenderer, self).render(*args, **kwargs)
        return compress_kml(kmldata)


class SVGRenderer(TemplateRenderer):
    """Renderer which serializes to SVG."""
    media_type = 'image/svg+xml'
    format = 'svg'
    template_name = 'spillway/features.svg'


class BaseGDALRenderer(BaseRenderer):
    """Abstract renderer which encodes to a GDAL supported raster format."""
    media_type = 'application/octet-stream'
    format = None

    @property
    def file_ext(self):
        return os.extsep + os.path.splitext(self.format)[0]

    def basename(self, item):
        """Returns the output filename.

        Arguments:
        item -- dict containing 'path'
        """
        fname = os.path.basename(item['path'])
        return os.path.splitext(fname)[0] + self.file_ext

    def render(self, data, accepted_media_type=None, renderer_context=None):
        if isinstance(data, dict):
            data = [data]
        self.set_filename(self.basename(data[0]), renderer_context)
        img = self._render_items(data, renderer_context)[0]
        # File contents could contain null bytes but not file names.
        try:
            isfile = os.path.isfile(img)
        except TypeError:
            isfile = False
        if isfile:
            self.set_response_length(os.path.getsize(img), renderer_context)
            img = open(img)
        return FileWrapper(img)

    def _render_items(self, items, renderer_context):
        renderer_context = renderer_context or {}
        view = renderer_context.get('view')
        geom = view and view.clean_params().get('g')
        driver = driver_for_path(self.file_ext.replace(os.extsep, ''))
        if geom:
            # Convert to wkb for ogr.Geometry
            geom = Geometry(wkb=bytes(geom.wkb), srs=geom.srs.wkt)
        imgdata = []
        for item in items:
            imgpath = item['path']
            # No conversion is needed if the original format without clipping
            # is requested.
            if not geom and imgpath.endswith(self.file_ext):
                imgdata.append(imgpath)
                continue
            memio = MemFileIO()
            if geom:
                with Raster(imgpath) as r:
                    with r.clip(geom) as clipped:
                        clipped.save(memio, driver)
            else:
                driver.copy(imgpath, memio.name)
            imgdata.append(memio.read())
            memio.close()
        return imgdata

    def set_filename(self, name, renderer_context):
        type_name = 'attachment; filename=%s.%s' % (name, self.format)
        try:
            renderer_context['response']['Content-Disposition'] = type_name
        except (KeyError, TypeError):
            pass

    def set_response_length(self, length, renderer_context):
        try:
            renderer_context['response']['Content-Length'] = length
        except (KeyError, TypeError):
            pass


class HFARenderer(BaseGDALRenderer):
    """Renders a raster to Erdas Imagine (.img) format."""
    format = 'img'


class GeoTIFFRenderer(BaseGDALRenderer):
    """Renders a raster to GeoTIFF (.tif) format."""
    media_type = 'image/tiff'
    format = 'tif'


class GeoTIFFZipRenderer(BaseGDALRenderer):
    """Bundles GeoTIFF rasters in a zip archive."""
    media_type = 'application/zip'
    format = 'tif.zip'
    arcdirname = 'data'

    def render(self, data, accepted_media_type=None, renderer_context=None):
        if isinstance(data, dict):
            data = [data]
        rendered = self._render_items(data, renderer_context)
        self.set_filename(self.arcdirname, renderer_context)
        fp = tempfile.TemporaryFile(suffix=os.extsep + self.format)
        with zipfile.ZipFile(fp, mode='w') as zf:
            for raster, attrs in zip(rendered, data):
                arcname = os.path.join(self.arcdirname, self.basename(attrs))
                # Attempt to write from the filename first, or fall back to the
                # file contents.
                try:
                    zf.write(raster, arcname=arcname)
                except TypeError:
                    zf.writestr(arcname, raster)
        self.set_response_length(fp.tell(), renderer_context)
        fp.seek(0)
        return FileWrapper(fp)


class HFAZipRenderer(GeoTIFFZipRenderer):
    """Bundles Erdas Imagine rasters in a zip archive."""
    format = 'img.zip'


class MapnikRenderer(BaseRenderer):
    mapfile = os.path.join(settings.MEDIA_ROOT, 'maptest.xml')
    media_type = 'image/png'
    format = 'png'

    def __init__(self, *args, **kwargs):
        super(MapnikRenderer, self).__init__(*args, **kwargs)
        m = mapnik.Map(256, 256)
        m.buffer_size = 128
        mapnik.load_map(m, self.mapfile)
        self.map = m

    def render(self, object, accepted_media_type=None, renderer_context=None):
        try:
            object.draw(self.map)
        except AttributeError:
            pass
        bbox = renderer_context.get('bbox')
        if bbox:
            bbox.transform(self.map.srs)
            self.map.zoom_to_box(mapnik.Box2d(*bbox.extent))
        img = mapnik.Image(self.map.width, self.map.height)
        mapnik.render(self.map, img)
        return img.tostring(self.format)

import re
import itertools
from lxml import html
from spatialist.vector import Vector, wkt2vector, bbox
from spatialist.auxil import utm_autodetect
from S1_NRB.ancillary import get_max_ext, buffer_min_overlap


def tile_from_aoi(vector, kml, epsg=None, strict=True, return_geometries=False, tilenames=None):
    """
    Return a list of MGRS tile IDs or vector objects overlapping one or multiple areas of interest.
    
    Parameters
    -------
    vector: spatialist.vector.Vector or list[spatialist.vector.Vector]
        The vector object(s) to read. CRS must be EPSG:4236.
    kml: str
        Path to the Sentinel-2 tiling grid KML file.
    epsg: int or list[int] or None
        Define which EPSG code(s) are allowed for the tile selection.
        If None, all tile IDs are returned regardless of projection.
    strict: bool
        Strictly only return the names/geometries of the overlapping tiles in the target projection
        or also allow reprojection of neighbouring tiles?
        In the latter case a tile name takes the form <tile ID>_<EPSG code>, e.g. `33TUN_32632`.
        Only applies if argument `epsg` is of type `int` or a list with one element.
    return_geometries: bool
        return a list of :class:`spatialist.vector.Vector` geometry objects (or just the tile names)?
    tilenames: list[str] or None
        an optional list of MGRS tile names to limit the selection
    
    Returns
    -------
    tiles: list[str or spatialist.vector.Vector]
        A list of unique MGRS tile IDs or :class:`spatialist.vector.Vector`
        objects with an attribute `mgrs` containing the tile ID.
    
    Notes
    -----
    The global Sentinel-2 tiling grid can be retrieved from:
    https://sentinel.esa.int/documents/247904/1955685/S2A_OPER_GIP_TILPAR_MPC__20151209T095117_V20150622T000000_21000101T000000_B00.kml
    """
    if isinstance(epsg, int):
        epsg = [epsg]
    if not isinstance(vector, list):
        vectors = [vector]
    else:
        vectors = vector
    for vector in vectors:
        if vector.getProjection('epsg') != 4326:
            raise RuntimeError('the CRS of the input vector object(s) must be EPSG:4326')
    sortkey = None
    if return_geometries:
        sortkey = lambda x: x.mgrs
    with Vector(kml, driver='KML') as vec:
        tilenames_src = []
        tiles = []
        for vector in vectors:
            vector.layer.ResetReading()
            for item in vector.layer:
                geom = item.GetGeometryRef()
                vec.layer.SetSpatialFilter(geom)
                for tile in vec.layer:
                    tilename = tile.GetField('Name')
                    c1 = tilename not in tilenames_src
                    c2 = tilenames is None or tilename in tilenames
                    if c1 and c2:
                        tilenames_src.append(tilename)
                        attrib = description2dict(tile.GetField('Description'))
                        reproject = False
                        if epsg is not None and attrib['EPSG'] not in epsg:
                            if len(epsg) == 1 and not strict:
                                epsg_target = int(epsg[0])
                                tilename += '_{}'.format(epsg_target)
                                reproject = True
                            else:
                                continue
                        if return_geometries:
                            if reproject:
                                with wkt2vector(attrib['UTM_WKT'], attrib['EPSG']) as tmp:
                                    tmp.reproject(epsg_target)
                                    ext = tmp.extent
                                    for k, v in ext.items():
                                        ext[k] = round(v / 10) * 10
                                geom = bbox(ext, crs=epsg_target)
                            else:
                                geom = wkt2vector(attrib['UTM_WKT'], attrib['EPSG'])
                            geom.mgrs = tilename
                            tiles.append(geom)
                        else:
                            tiles.append(tilename)
            vector.layer.ResetReading()
        tile = None
        geom = None
        item = None
        return sorted(tiles, key=sortkey)


def aoi_from_tile(kml, tile):
    """
    Extract one or multiple MGRS tiles from the global Sentinel-2 tiling grid and return it as a :class:`~spatialist.vector.Vector`
    object.
    
    Parameters
    ----------
    kml: str
        Path to the Sentinel-2 tiling grid KML file.
    tile: str or list[str]
        The MGRS tile ID(s) that should be extracted and returned as a vector object.
        Can also be expressed as <tile ID>_<EPSG code> (e.g. `33TUN_32632`). In this case the geometry
        of the tile is reprojected to the target EPSG code, its corner coordinates rounded to multiples
        of 10, and a new :class:`~spatialist.vector.Vector` object created.
    
    Returns
    -------
    spatialist.vector.Vector or list[spatialist.vector.Vector]
        either a single object or a list depending on `tile`
    
    Notes
    -----
    The global Sentinel-2 tiling grid can be retrieved from:
    https://sentinel.esa.int/documents/247904/1955685/S2A_OPER_GIP_TILPAR_MPC__20151209T095117_V20150622T000000_21000101T000000_B00.kml
    """
    if isinstance(tile, list):
        return [aoi_from_tile(kml=kml, tile=x) for x in tile]
    else:
        tilename, epsg = re.search('([A-Z0-9]{5})_?([0-9]+)?', tile).groups()
        with Vector(kml, driver='KML') as vec:
            feat = vec.getFeatureByAttribute('Name', tilename)
            attrib = description2dict(feat.GetField('Description'))
            feat = None
        if epsg is None:
            return wkt2vector(attrib['UTM_WKT'], attrib['EPSG'])
        else:
            with wkt2vector(attrib['UTM_WKT'], attrib['EPSG']) as tmp:
                tmp.reproject(int(epsg))
                ext = tmp.extent
                for k, v in ext.items():
                    ext[k] = round(v / 10) * 10
            return bbox(ext, crs=int(epsg))


def description2dict(description):
    """
    Convert the HTML description field of the MGRS tile KML file to a dictionary.
    
    Parameters
    ----------
    description: str
        The plain text of the `Description` field
    
    Returns
    -------
    attrib: dict
        A dictionary with keys 'TILE_ID', 'EPSG', 'MGRS_REF', 'UTM_WKT' and 'LL_WKT'.
        The value of field 'EPSG' is of type integer, all others are strings.
    """
    attrib = html.fromstring(description)
    attrib = [x for x in attrib.xpath('//tr/td//text()') if x != ' ']
    attrib = dict(zip(attrib[0::2], attrib[1::2]))
    attrib['EPSG'] = int(attrib['EPSG'])
    return attrib


def aoi_from_scene(scene, kml, multi=True):
    """
    Get processing AOIs for a SAR scene. The MGRS grid requires a SAR scene to be geocoded to multiple UTM zones
    depending on the overlapping MGRS tiles and their projection. This function returns the following for each
    UTM zone group:
    
    - the extent in WGS84 coordinates (key `extent`)
    - the EPSG code of the UTM zone (key `epsg`)
    - the Easting coordinate for pixel alignment (key `align_x`)
    - the Northing coordinate for pixel alignment (key `align_y`)
    
    Parameters
    ----------
    scene: pyroSAR.drivers.ID
        the SAR scene object
    kml: str
        Path to the Sentinel-2 tiling grid KML file.
    multi: bool
        split into multiple AOIs per overlapping UTM zone or just one AOI covering the whole scene.
        In the latter case the best matching UTM zone is auto-detected
        (using function :func:`spatialist.auxil.utm_autodetect`).

    Returns
    -------
    list[dict]
        a list of dictionaries with keys `extent`, `epsg`, `align_x`, `align_y`
    """
    out = []
    if multi:
        # extract all overlapping tiles
        with scene.geometry() as geom:
            tiles = tile_from_aoi(vector=geom, kml=kml, return_geometries=True)
        # group tiles by UTM zone
        for zone, group in itertools.groupby(tiles, lambda x: x.mgrs[:2]):
            geometries = list(group)
            # get UTM EPSG code
            epsg = geometries[0].getProjection(type='epsg')
            # get maximum extent of tile group
            ext = get_max_ext(geometries=geometries)
            # determine corner coordinate for alignment
            align_x = ext['xmin']
            align_y = ext['ymax']
            del geometries
            # convert extent to EPSG:4326
            with bbox(coordinates=ext, crs=epsg) as geom:
                geom.reproject(projection=4326)
                ext = geom.extent
            # ensure a minimum overlap between AOI and pre-processed scene
            with bbox(ext, 4326) as geom1:
                with scene.geometry() as geom2:
                    with buffer_min_overlap(geom1=geom1, geom2=geom2) as buffered:
                        ext = buffered.extent
            out.append({'extent': ext, 'epsg': epsg,
                        'align_x': align_x, 'align_y': align_y})
    else:
        with scene.bbox() as geom:
            ext = geom.extent
            # auto-detect UTM zone
            epsg = utm_autodetect(geom, 'epsg')
            # get all tiles, reprojected to the target UTM zone if necessary
            tiles = tile_from_aoi(vector=geom, kml=kml, epsg=epsg,
                                  return_geometries=True, strict=False)
        # determine corner coordinate for alignment
        ext_utm = tiles[0].extent
        align_x = ext_utm['xmin']
        align_y = ext_utm['ymax']
        del tiles
        out.append({'extent': ext, 'epsg': epsg,
                    'align_x': align_x, 'align_y': align_y})
    return out

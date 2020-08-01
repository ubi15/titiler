"""API for MosaicJSON Dataset."""

import os
import re
from typing import Dict, Optional
from urllib.parse import urlencode

import morecantile
from cogeo_mosaic.backends import MosaicBackend
from cogeo_mosaic.mosaic import MosaicJSON
from cogeo_mosaic.utils import get_footprints
from rio_tiler.constants import MAX_THREADS
from rio_tiler_crs.cogeo import geotiff_options

from titiler import utils
from titiler.dependencies import CommonTileParams, MosaicPath
from titiler.endpoints.cog import tile_response_codes
from titiler.errors import BadRequestError, TileNotFoundError
from titiler.models.cog import cogBounds
from titiler.models.mapbox import TileJSON
from titiler.models.mosaic import CreateMosaicJSON, UpdateMosaicJSON, mosaicInfo
from titiler.ressources.enums import (
    ImageMimeTypes,
    ImageType,
    MimeTypes,
    PixelSelectionMethod,
)
from titiler.ressources.responses import XMLResponse

from fastapi import APIRouter, Depends, Path, Query

from starlette.requests import Request
from starlette.responses import Response
from starlette.templating import Jinja2Templates

router = APIRouter()
templates = Jinja2Templates(directory="titiler/templates")


@router.post("", response_model=MosaicJSON, response_model_exclude_none=True)
def create_mosaicjson(body: CreateMosaicJSON):
    """Create a MosaicJSON"""
    mosaic = MosaicJSON.from_urls(
        body.files,
        minzoom=body.minzoom,
        maxzoom=body.maxzoom,
        max_threads=body.max_threads,
    )
    mosaic_path = MosaicPath(body.url)
    with MosaicBackend(mosaic_path, mosaic_def=mosaic) as mosaic:
        try:
            mosaic.write()
        except NotImplementedError:
            raise BadRequestError(
                f"{mosaic.__class__.__name__} does not support write operations"
            )
        return mosaic.mosaic_def


@router.get(
    "",
    response_model=MosaicJSON,
    response_model_exclude_none=True,
    responses={200: {"description": "Return MosaicJSON definition"}},
)
def read_mosaicjson(mosaic_path: str = Depends(MosaicPath)):
    """Read a MosaicJSON"""
    with MosaicBackend(mosaic_path) as mosaic:
        return mosaic.mosaic_def


@router.put("", response_model=MosaicJSON, response_model_exclude_none=True)
def update_mosaicjson(body: UpdateMosaicJSON):
    """Update an existing MosaicJSON"""
    mosaic_path = MosaicPath(body.url)
    with MosaicBackend(mosaic_path) as mosaic:
        features = get_footprints(body.files, max_threads=body.max_threads)
        try:
            mosaic.update(features, add_first=body.add_first, quiet=True)
        except NotImplementedError:
            raise BadRequestError(
                f"{mosaic.__class__.__name__} does not support update operations"
            )
        return mosaic.mosaic_def


@router.get(
    "/bounds",
    response_model=cogBounds,
    responses={200: {"description": "Return the bounds of the MosaicJSON"}},
)
def mosaicjson_bounds(mosaic_path: str = Depends(MosaicPath)):
    """Read MosaicJSON bounds"""
    with MosaicBackend(mosaic_path) as mosaic:
        return {"bounds": mosaic.mosaic_def.bounds}


@router.get("/info", response_model=mosaicInfo)
def mosaicjson_info(mosaic_path: str = Depends(MosaicPath)):
    """
    Read MosaicJSON info

    Ref: https://github.com/developmentseed/cogeo-mosaic-tiler/blob/master/cogeo_mosaic_tiler/handlers/app.py#L164-L198
    """
    with MosaicBackend(mosaic_path) as mosaic:
        meta = mosaic.metadata
        response = {
            "bounds": meta["bounds"],
            "center": meta["center"],
            "maxzoom": meta["maxzoom"],
            "minzoom": meta["minzoom"],
            "name": mosaic_path,
            "quadkeys": list(mosaic.mosaic_def.tiles),
        }
        return response


@router.get(
    "/tilejson.json",
    response_model=TileJSON,
    responses={200: {"description": "Return a tilejson"}},
    response_model_exclude_none=True,
)
def mosaic_tilejson(
    request: Request,
    tile_scale: int = Query(
        1, gt=0, lt=4, description="Tile size scale. 1=256x256, 2=512x512..."
    ),
    tile_format: Optional[ImageType] = Query(
        None, description="Output image type. Default is auto."
    ),
    mosaic_path: str = Depends(MosaicPath),
):
    """Create TileJSON"""
    kwargs = {"z": "{z}", "x": "{x}", "y": "{y}", "scale": tile_scale}
    if tile_format:
        kwargs["format"] = tile_format
    tile_url = request.url_for("mosaic_tile", **kwargs).replace("\\", "")

    with MosaicBackend(mosaic_path) as mosaic:
        tjson = TileJSON(**mosaic.metadata, tiles=[tile_url])

    return tjson


@router.get(
    r"/point/{lon},{lat}",
    responses={200: {"description": "Return a value for a point"}},
)
async def mosaic_point(
    lon: float = Path(..., description="Longitude"),
    lat: float = Path(..., description="Latitude"),
    bidx: Optional[str] = Query(
        None, title="Band indexes", description="comma (',') delimited band indexes",
    ),
    expression: Optional[str] = Query(
        None,
        title="Band Math expression",
        description="rio-tiler's band math expression (e.g B1/B2)",
    ),
    mosaic_path: str = Depends(MosaicPath),
):
    """Get Point value for a MosaicJSON."""
    indexes = tuple(int(s) for s in re.findall(r"\d+", bidx)) if bidx else None

    timings = []
    headers: Dict[str, str] = {}
    threads = int(os.getenv("MOSAIC_CONCURRENCY", MAX_THREADS))

    with utils.Timer() as t:
        with MosaicBackend(mosaic_path) as mosaic:
            values = mosaic.point(lon, lat, indexes=indexes, threads=threads)

    timings.append(("Read-values", t.elapsed))

    if timings:
        headers["X-Server-Timings"] = "; ".join(
            ["{} - {:0.2f}".format(name, time * 1000) for (name, time) in timings]
        )

    return {"coordinates": [lon, lat], "values": values}


@router.get(r"/tiles/{z}/{x}/{y}", **tile_response_codes)
@router.get(r"/tiles/{z}/{x}/{y}.{format}", **tile_response_codes)
@router.get(r"/tiles/{z}/{x}/{y}@{scale}x", **tile_response_codes)
@router.get(r"/tiles/{z}/{x}/{y}@{scale}x.{format}", **tile_response_codes)
@router.get(r"/tiles/WebMercatorQuad/{z}/{x}/{y}.{format}", **tile_response_codes)
@router.get(r"/tiles/WebMercatorQuad/{z}/{x}/{y}@{scale}x", **tile_response_codes)
@router.get(
    r"/tiles/WebMercatorQuad/{z}/{x}/{y}@{scale}x.{format}", **tile_response_codes
)
async def mosaic_tile(
    z: int = Path(..., ge=0, le=30, description="Mercator tiles's zoom level"),
    x: int = Path(..., description="Mercator tiles's column"),
    y: int = Path(..., description="Mercator tiles's row"),
    scale: int = Query(
        1, gt=0, lt=4, description="Tile size scale. 1=256x256, 2=512x512..."
    ),
    format: ImageType = Query(None, description="Output image type. Default is auto."),
    pixel_selection: PixelSelectionMethod = Query(
        PixelSelectionMethod.first, description="Pixel selection method."
    ),
    image_params: CommonTileParams = Depends(),
    mosaic_path: str = Depends(MosaicPath),
):
    """Read MosaicJSON tile"""
    timings = []
    headers: Dict[str, str] = {}

    tilesize = 256 * scale
    threads = int(os.getenv("MOSAIC_CONCURRENCY", MAX_THREADS))

    with utils.Timer() as t:
        with MosaicBackend(mosaic_path) as mosaic:
            (tile, mask), assets_used = mosaic.tile(
                x,
                y,
                z,
                pixel_selection=pixel_selection.method(),
                threads=threads,
                tilesize=tilesize,
                indexes=image_params.indexes,
                expression=image_params.expression,
                nodata=image_params.nodata,
                **image_params.kwargs,
            )

    timings.append(("Read-tile", t.elapsed))

    if tile is None:
        raise TileNotFoundError(f"Tile {z}/{x}/{y} was not found")

    if not format:
        format = ImageType.jpg if mask.all() else ImageType.png

    with utils.Timer() as t:
        tile = utils.postprocess(
            tile,
            mask,
            rescale=image_params.rescale,
            color_formula=image_params.color_formula,
        )
    timings.append(("Post-process", t.elapsed))

    opts = {}
    if ImageType.tif in format:
        opts = geotiff_options(x, y, z, tilesize=tilesize)

    with utils.Timer() as t:
        content = utils.reformat(
            tile, mask, img_format=format, colormap=image_params.color_map, **opts
        )
    timings.append(("Format", t.elapsed))

    if timings:
        headers["X-Server-Timings"] = "; ".join(
            ["{} - {:0.2f}".format(name, time * 1000) for (name, time) in timings]
        )

    if assets_used:
        headers["X-Assets"] = ",".join(assets_used)

    return Response(
        content, media_type=ImageMimeTypes[format.value].value, headers=headers
    )


@router.get("/WMTSCapabilities.xml", response_class=XMLResponse, tags=["OGC"])
def wmts(
    request: Request,
    tile_format: ImageType = Query(
        ImageType.png, description="Output image type. Default is png."
    ),
    tile_scale: int = Query(
        1, gt=0, lt=4, description="Tile size scale. 1=256x256, 2=512x512..."
    ),
    mosaic_path: str = Depends(MosaicPath),
):
    """OGC WMTS endpoint."""
    endpoint = request.url_for("read_mosaicjson")

    kwargs = dict(request.query_params)
    kwargs.pop("tile_format", None)
    kwargs.pop("tile_scale", None)
    qs = urlencode(list(kwargs.items()))

    tms = morecantile.tms.get("WebMercatorQuad")
    with MosaicBackend(mosaic_path) as mosaic:
        minzoom = mosaic.mosaic_def.minzoom
        maxzoom = mosaic.mosaic_def.maxzoom
        bounds = mosaic.mosaic_def.bounds

    media_type = ImageMimeTypes[tile_format.value].value

    tileMatrix = []
    for zoom in range(minzoom, maxzoom + 1):
        matrix = tms.matrix(zoom)
        tm = f"""
                <TileMatrix>
                    <ows:Identifier>{matrix.identifier}</ows:Identifier>
                    <ScaleDenominator>{matrix.scaleDenominator}</ScaleDenominator>
                    <TopLeftCorner>{matrix.topLeftCorner[0]} {matrix.topLeftCorner[1]}</TopLeftCorner>
                    <TileWidth>{matrix.tileWidth}</TileWidth>
                    <TileHeight>{matrix.tileHeight}</TileHeight>
                    <MatrixWidth>{matrix.matrixWidth}</MatrixWidth>
                    <MatrixHeight>{matrix.matrixHeight}</MatrixHeight>
                </TileMatrix>"""
        tileMatrix.append(tm)

    tile_ext = f"@{tile_scale}x.{tile_format.value}"
    return templates.TemplateResponse(
        "wmts.xml",
        {
            "request": request,
            "endpoint": endpoint,
            "bounds": bounds,
            "tileMatrix": tileMatrix,
            "tms": tms,
            "title": "Cloud Optimized GeoTIFF",
            "query_string": qs,
            "tile_format": tile_ext,
            "media_type": media_type,
        },
        media_type=MimeTypes.xml.value,
    )

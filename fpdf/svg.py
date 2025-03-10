import math
import re
import warnings
from typing import NamedTuple

try:
    from svg.path import (
        parse_path,
        Move,
        Close,
        Line,
        CubicBezier,
        QuadraticBezier,
        Arc,
    )
except ImportError:
    warnings.warn(
        "svg.path could not be imported - fpdf2 will not be able to render SVG images"
    )
    parse_path = None

try:
    from defusedxml.ElementTree import fromstring as parse_xml_str
except ImportError:
    warnings.warn(
        "defusedxml could not be imported - fpdf2 will not be able to sanitize SVG images provided"
    )
    from xml.etree.ElementTree import fromstring as parse_xml_str  # nosec

from . import html
from .drawing import (
    color_from_hex_string,
    BezierCurve,
    GraphicsContext,
    GraphicsStyle,
    PaintedPath,
    Point,
    QuadraticBezierCurve,
    Transform,
)

__pdoc__ = {"force_nodocument": False}


def force_nodocument(item):
    """A decorator that forces pdoc not to document the decorated item (class or method)"""
    __pdoc__[item.__qualname__] = False
    return item


# https://www.w3.org/TR/SVG/Overview.html

_HANDY_NAMESPACES = {
    "svg": "http://www.w3.org/2000/svg",
    "xlink": "http://www.w3.org/1999/xlink",
}

NUMBER_SPLIT = re.compile(r"(?:\s+,\s+|\s+,|,\s+|\s+|,)")
TRANSFORM_GETTER = re.compile(
    r"(matrix|rotate|scale|scaleX|scaleY|skew|skewX|skewY|translate|translateX|translateY)"
    r"\(((?:\s*(?:[-+]?[\d\.]+,?)+\s*)+)\)"
)


@force_nodocument
class Percent(float):
    """class to represent percentage values"""


unit_splitter = re.compile(r"\s*(?P<value>[-+]?[\d\.]+)\s*(?P<unit>%|[a-zA-Z]*)")

# none of these are supported right now
# https://www.w3.org/TR/css-values-4/#lengths
relative_length_units = {
    "%",  # (context sensitive, depends on which attribute it is applied to)
    "em",  # (current font size)
    "ex",  # (current font x-height)
    # CSS 3
    "ch",  # (advance measure of 0, U+0030 glyph)
    "rem",  # (font-size of the root element)
    "vw",  # (1% of viewport width)
    "vh",  # (1% of viewport height)
    "vmin",  # (smaller of vw or vh)
    "vmax",  # (larger of vw or vh)
    # CSS 4
    "cap",  # (font cap height)
    "ic",  # (advance measure of fullwidth U+6C34 glyph)
    "lh",  # (line height)
    "rlh",  # (root element line height)
    "vi",  # (1% of viewport size in root element's inline axis)
    "vb",  # (1% of viewport size in root element's block axis)
}

absolute_length_units = {
    "in": 72,  # (inches, 72 pt)
    "cm": 72 / 2.54,  # (centimeters, 72 / 2.54 pt)
    "mm": 72 / 25.4,  # (millimeters 72 / 25.4 pt)
    "pt": 1,  # (pdf canonical unit)
    "pc": 12,  # (pica, 12 pt)
    "px": 0.75,  # (reference pixel unit, 0.75 pt)
    # CSS 3
    "Q": 72 / 101.6,  # (quarter-millimeter, 72 / 101.6 pt)
}

angle_units = {
    "deg": math.tau / 360,
    "grad": math.tau / 400,
    "rad": 1,  # pdf canonical unit
    "turn": math.tau,
}


# in CSS the default length unit is px, but as far as I can tell, for SVG interpreting
# unitless numbers as being expressed in pt is more appropriate. Particularly, the
# scaling we do using viewBox attempts to scale so that 1 svg user unit = 1 pdf pt
# because this results in the output PDF having the correct physical dimensions (i.e. a
# feature with a 1cm size in SVG will actually end up being 1cm in size in the PDF).
@force_nodocument
def resolve_length(length_str, default_unit="pt"):
    """Convert a length unit to our canonical length unit, pt."""
    value, unit = unit_splitter.match(length_str).groups()
    if not unit:
        unit = default_unit

    try:
        return float(value) * absolute_length_units[unit]
    except KeyError:
        if unit in relative_length_units:
            raise ValueError(
                f"{length_str} uses unsupported relative length {unit}"
            ) from None

        raise ValueError(f"{length_str} contains unrecognized unit {unit}") from None


@force_nodocument
def resolve_angle(angle_str, default_unit="deg"):
    """Convert an angle value to our canonical angle unit, radians"""
    value, unit = unit_splitter.match(angle_str).groups()
    if not unit:
        unit = default_unit

    try:
        return float(value) * angle_units[unit]
    except KeyError:
        raise ValueError(f"angle {angle_str} has unknown unit {unit}") from None


@force_nodocument
def xmlns(space, name):
    """Create an XML namespace string representation for the given tag name."""
    try:
        space = f"{{{_HANDY_NAMESPACES[space]}}}"
    except KeyError:
        space = ""

    return f"{space}{name}"


@force_nodocument
def xmlns_lookup(space, *names):
    """Create a lookup for the given name in the given XML namespace."""

    result = {}
    for name in names:
        result[xmlns(space, name)] = name
        result[name] = name

    return result


shape_tags = xmlns_lookup(
    "svg", "rect", "circle", "ellipse", "line", "polyline", "polygon"
)


@force_nodocument
def svgcolor(colorstr):
    try:
        colorstr = html.COLOR_DICT[colorstr]
    except KeyError:
        pass

    if colorstr.startswith("#"):
        return color_from_hex_string(colorstr)

    raise ValueError(f"unsupported color specification {colorstr}")


@force_nodocument
def convert_stroke_width(incoming):
    val = float(incoming)
    if val < 0:
        raise ValueError(f"stroke width {incoming} cannot be negative")
    if val == 0:
        return None

    return val


@force_nodocument
def convert_miterlimit(incoming):
    val = float(incoming)
    if val < 1.0:
        raise ValueError(f"miter limit {incoming} cannot be less than 1")

    return val


@force_nodocument
def clamp_float(min_val, max_val):
    def converter(value):
        val = float(value)
        if val < min_val:
            return min_val
        if val > max_val:
            return max_val
        return val

    return converter


@force_nodocument
def inheritable(value, converter=lambda value: value):
    if value == "inherit":
        return GraphicsStyle.INHERIT

    return converter(value)


@force_nodocument
def optional(value, converter=lambda noop: noop):
    if value == "none":
        return None

    return inheritable(value, converter)


# this is mostly SVG 1.1 stuff. SVG 2 changed some of this and the documentation is much
# harder to assemble into something coherently understandable
svg_attr_map = {
    # https://www.w3.org/TR/SVG11/painting.html#FillProperty
    "fill": lambda colorstr: ("fill_color", optional(colorstr, svgcolor)),
    # https://www.w3.org/TR/SVG11/painting.html#FillRuleProperty
    "fill-rule": lambda fillrulestr: ("intersection_rule", inheritable(fillrulestr)),
    # https://www.w3.org/TR/SVG11/painting.html#FillOpacityProperty
    "fill-opacity": lambda filopstr: (
        "fill_opacity",
        inheritable(filopstr, clamp_float(0.0, 1.0)),
    ),
    # https://www.w3.org/TR/SVG11/painting.html#StrokeProperty
    "stroke": lambda colorstr: ("stroke_color", optional(colorstr, svgcolor)),
    # https://www.w3.org/TR/SVG11/painting.html#StrokeWidthProperty
    "stroke-width": lambda valuestr: (
        "stroke_width",
        inheritable(valuestr, convert_stroke_width),
    ),
    # https://www.w3.org/TR/SVG11/painting.html#StrokeDasharrayProperty
    "stroke-dasharray": lambda dasharray: (
        "stroke_dash_pattern",
        optional(
            dasharray, lambda da: [float(item) for item in NUMBER_SPLIT.split(da)]
        ),
    ),
    # stroke-dashoffset may be a percentage, which we don't support currently
    # https://www.w3.org/TR/SVG11/painting.html#StrokeDashoffsetProperty
    "stroke-dashoffset": lambda dashoff: (
        "stroke_dash_phase",
        inheritable(dashoff, float),
    ),
    # https://www.w3.org/TR/SVG11/painting.html#StrokeLinecapProperty
    "stroke-linecap": lambda capstr: ("stroke_cap_style", inheritable(capstr)),
    # https://www.w3.org/TR/SVG11/painting.html#StrokeLinejoinProperty
    "stroke-linejoin": lambda joinstr: ("stroke_join_style", inheritable(joinstr)),
    # https://www.w3.org/TR/SVG11/painting.html#StrokeMiterlimitProperty
    "stroke-miterlimit": lambda limstr: (
        "stroke_miter_limit",
        inheritable(limstr, convert_miterlimit),
    ),
    # https://www.w3.org/TR/SVG11/painting.html#StrokeOpacityProperty
    "stroke-opacity": lambda stropstr: (
        "stroke_opacity",
        inheritable(stropstr, clamp_float(0.0, 1.0)),
    ),
}


@force_nodocument
def parse_style(svg_element):
    """Parse `style="..."` making it's key-value pairs element's attributes"""
    try:
        style = svg_element.attrib["style"]
    except KeyError:
        pass
    else:
        for element in style.split(";"):
            if not element:
                continue

            pair = element.split(":")
            if len(pair) == 2 and pair[0] and pair[1]:
                attr, value = pair

                svg_element.attrib[attr.strip()] = value.strip()


@force_nodocument
def apply_styles(stylable, svg_element):
    """Apply the known styles from `svg_element` to the pdf path/group `stylable`."""
    parse_style(svg_element)

    stylable.style.auto_close = False

    for svg_attr, converter in svg_attr_map.items():
        try:
            attr, value = converter(svg_element.attrib[svg_attr])
        except KeyError:
            pass
        else:
            setattr(stylable.style, attr, value)

    # handle this separately for now
    try:
        opacity = float(svg_element.attrib["opacity"])
    except KeyError:
        pass
    else:
        stylable.style.fill_opacity = opacity
        stylable.style.stroke_opacity = opacity

    try:
        tfstr = svg_element.attrib["transform"]
    except KeyError:
        pass
    else:
        stylable.transform = convert_transforms(tfstr)


@force_nodocument
class ShapeBuilder:
    """A namespace within which methods for converting basic shapes can be looked up."""

    @staticmethod
    def new_path(tag):
        """Create a new path with the appropriate styles."""
        path = PaintedPath()
        apply_styles(path, tag)

        return path

    @classmethod
    def rect(cls, tag):
        """Convert an SVG <rect> into a PDF path."""
        # svg rect is wound clockwise
        x = float(tag.attrib.get("x", 0))
        y = float(tag.attrib.get("y", 0))
        width = float(tag.attrib.get("width", 0))
        height = float(tag.attrib.get("height", 0))
        rx = tag.attrib.get("rx", "auto")
        ry = tag.attrib.get("ry", "auto")

        if rx == "none":
            rx = 0
        if ry == "none":
            ry = 0

        if rx == ry == "auto":
            rx = ry = 0
        elif rx == "auto":
            rx = ry = float(ry)
        elif ry == "auto":
            ry = rx = float(rx)
        else:
            rx = float(rx)
            ry = float(ry)

        if (width < 0) or (height < 0) or (rx < 0) or (ry < 0):
            raise ValueError(f"bad rect {tag}")

        if (width == 0) or (height == 0):
            return PaintedPath()

        if rx > (width / 2):
            rx = width / 2
        if ry > (height / 2):
            ry = height / 2

        path = cls.new_path(tag)

        path.rectangle(x, y, width, height, rx, ry)
        return path

    @classmethod
    def circle(cls, tag):
        """Convert an SVG <circle> into a PDF path."""
        cx = float(tag.attrib.get("cx", 0))
        cy = float(tag.attrib.get("cy", 0))
        r = float(tag.attrib["r"])

        path = cls.new_path(tag)

        path.circle(cx, cy, r)
        return path

    @classmethod
    def ellipse(cls, tag):
        """Convert an SVG <ellipse> into a PDF path."""
        cx = float(tag.attrib.get("cx", 0))
        cy = float(tag.attrib.get("cy", 0))

        rx = tag.attrib.get("rx", "auto")
        ry = tag.attrib.get("ry", "auto")

        path = cls.new_path(tag)

        if (rx == ry == "auto") or (rx == 0) or (ry == 0):
            return path

        if rx == "auto":
            rx = ry = float(ry)
        elif ry == "auto":
            rx = ry = float(rx)
        else:
            rx = float(rx)
            ry = float(ry)

        path.ellipse(cx, cy, rx, ry)
        return path

    @classmethod
    def line(cls, tag):
        """Convert an SVG <line> into a PDF path."""
        x1 = float(tag.attrib["x1"])
        y1 = float(tag.attrib["y1"])
        x2 = float(tag.attrib["x2"])
        y2 = float(tag.attrib["y2"])

        path = cls.new_path(tag)

        path.move_to(x1, y1)
        path.line_to(x2, y2)

        return path

    @classmethod
    def polyline(cls, tag):
        """Convert an SVG <polyline> into a PDF path."""
        points = tag.attrib["points"]

        path = cls.new_path(tag)

        points = "M" + points
        svg_path_converter(path, points)

        return path

    @classmethod
    def polygon(cls, tag):
        """Convert an SVG <polygon> into a PDF path."""
        points = tag.attrib["points"]

        path = cls.new_path(tag)

        points = "M" + points + "Z"
        svg_path_converter(path, points)

        return path


@force_nodocument
def convert_transforms(tfstr):
    """Convert SVG/CSS transform functions into PDF transforms."""

    # SVG 2 uses CSS transforms. SVG 1.1 transforms are slightly different. I'm really
    # not sure if it is worth it to try to support SVG 2 because it is significantly
    # more entangled with The HTML Disaster than SVG 1.1, which makes it astronomically
    # harder to support.
    # https://drafts.csswg.org/css-transforms/#two-d-transform-functions
    parsed = TRANSFORM_GETTER.findall(tfstr)

    transform = Transform.identity()
    for tf_type, args in parsed:
        if tf_type == "matrix":
            a, b, c, d, e, f = tuple(float(n) for n in NUMBER_SPLIT.split(args))
            transform = Transform(a, b, c, d, e, f) @ transform

        elif tf_type == "rotate":
            theta, *about = NUMBER_SPLIT.split(args)
            theta = resolve_angle(theta)
            rotation = Transform.rotation(theta=theta)
            if about:
                # this is an SVG 1.1 feature. SVG 2 uses the transform-origin property.
                # see: https://www.w3.org/TR/SVG11/coords.html#TransformAttribute
                if len(about) == 2:
                    rotation = rotation.about(float(about[0]), float(about[1]))
                else:
                    raise ValueError(
                        f"rotation transform {tf_type}({args}) is malformed"
                    )

            transform = rotation @ transform

        elif tf_type == "scale":
            # if sy is not provided, it takes a value equal to sx
            args = NUMBER_SPLIT.split(args)
            if len(args) == 2:
                sx = float(args[0])
                sy = float(args[1])
            elif len(args) == 1:
                sx = sy = float(args[0])
            else:
                raise ValueError(f"bad scale transform {tfstr}")

            transform = Transform.scaling(x=sx, y=sy) @ transform

        elif tf_type == "scaleX":  # SVG 2
            transform = Transform.scaling(x=float(args), y=1) @ transform

        elif tf_type == "scaleY":  # SVG 2
            transform = Transform.scaling(x=1, y=float(args)) @ transform

        elif tf_type == "skew":  # SVG 2, not the same as skewX@skewY
            # if sy is not provided, it takes a value equal to 0
            args = NUMBER_SPLIT.split(args)
            if len(args) == 2:
                sx = resolve_angle(args[0])
                sy = resolve_angle(args[1])
            elif len(args) == 1:
                sx = resolve_angle(args[0])
                sy = 0
            else:
                raise ValueError(f"bad skew transform {tfstr}")

            transform = Transform.shearing(x=math.tan(sx), y=math.tan(sy)) @ transform

        elif tf_type == "skewX":
            transform = (
                Transform.shearing(x=math.tan(resolve_angle(args)), y=0) @ transform
            )

        elif tf_type == "skewY":
            transform = (
                Transform.shearing(x=0, y=math.tan(resolve_angle(args))) @ transform
            )

        elif tf_type == "translate":
            # if y is not provided, it takes a value equal to 0
            args = NUMBER_SPLIT.split(args)
            if len(args) == 2:
                x = resolve_length(args[0])
                y = resolve_length(args[1])
            elif len(args) == 1:
                x = resolve_length(args[0])
                y = 0
            else:
                raise ValueError(f"bad translation transform {tfstr}")

            transform = Transform.translation(x=x, y=y) @ transform

        elif tf_type == "translateX":  # SVG 2
            transform = Transform.translation(x=resolve_length(args), y=0) @ transform

        elif tf_type == "translateY":  # SVG 2
            transform = Transform.translation(x=0, y=resolve_length(args)) @ transform

    return transform


@force_nodocument
class SVGSmoothCubicCurve(NamedTuple):
    """SVG chained cubic Bézier curve path element."""

    c2: Point
    end: Point

    @classmethod
    def from_path_points(cls, path, c2x, c2y, ex, ey):
        return path.add_path_element(cls(c2=Point(x=c2x, y=c2y), end=Point(x=ex, y=ey)))

    def render(self, path_gsds, style, last_item, initial_point):
        # technically, it would also be possible to chain on from a quadratic Bézier,
        # since we can convert those to cubic curves and then retrieve the appropriate
        # control point. However, the SVG specification states in
        # https://www.w3.org/TR/SVG/paths.html#PathDataCubicBezierCommands
        # "if the previous command was not an C, c, S or s, assume the first control
        # point is coincident with the current point."
        if isinstance(last_item, BezierCurve):
            c1 = (2 * last_item.end) - last_item.c2
        else:
            c1 = last_item.end_point

        return BezierCurve(c1, self.c2, self.end).render(
            path_gsds, style, last_item, initial_point
        )

    def render_debug(
        self, path_gsds, style, last_item, initial_point, debug_stream, pfx
    ):
        # pylint: disable=unused-argument
        rendered, resolved, initial_point = self.render(
            path_gsds, style, last_item, initial_point
        )
        debug_stream.write(f"{self} resolved to {resolved}\n")

        return rendered, resolved, initial_point


@force_nodocument
class SVGRelativeSmoothCubicCurve(NamedTuple):
    """SVG chained relative cubic Bézier curve path element."""

    c2: Point
    end: Point

    @classmethod
    def from_path_points(cls, path, c2x, c2y, ex, ey):
        return path.add_path_element(cls(c2=Point(x=c2x, y=c2y), end=Point(x=ex, y=ey)))

    def render(self, path_gsds, style, last_item, initial_point):
        last_point = last_item.end_point

        if isinstance(last_item, BezierCurve):
            c1 = (2 * last_item.end) - last_item.c2
        else:
            c1 = last_point

        c2 = last_point + self.c2
        end = last_point + self.end

        return BezierCurve(c1, c2, end).render(
            path_gsds, style, last_item, initial_point
        )

    def render_debug(
        self, path_gsds, style, last_item, initial_point, debug_stream, pfx
    ):
        # pylint: disable=unused-argument
        rendered, resolved, initial_point = self.render(
            path_gsds, style, last_item, initial_point
        )
        debug_stream.write(f"{self} resolved to {resolved}\n")

        return rendered, resolved, initial_point


@force_nodocument
class SVGSmoothQuadraticCurve(NamedTuple):
    """SVG chained quadratic Bézier curve path element."""

    end: Point

    @classmethod
    def from_path_points(cls, path, ex, ey):
        return path.add_path_element(cls(end=Point(x=ex, y=ey)))

    def render(self, path_gsds, style, last_item, initial_point):
        if isinstance(last_item, QuadraticBezierCurve):
            ctrl = (2 * last_item.end) - last_item.ctrl
        else:
            ctrl = last_item.end_point

        return QuadraticBezierCurve(ctrl, self.end).render(
            path_gsds, style, last_item, initial_point
        )

    def render_debug(
        self, path_gsds, style, last_item, initial_point, debug_stream, pfx
    ):
        # pylint: disable=unused-argument
        rendered, resolved, initial_point = self.render(
            path_gsds, style, last_item, initial_point
        )
        debug_stream.write(f"{self} resolved to {resolved}\n")

        return rendered, resolved, initial_point


@force_nodocument
class SVGRelativeSmoothQuadraticCurve(NamedTuple):
    """SVG chained relative quadratic Bézier curve path element."""

    end: Point

    @classmethod
    def from_path_points(cls, path, ex, ey):
        return path.add_path_element(cls(end=Point(x=ex, y=ey)))

    def render(self, path_gsds, style, last_item, initial_point):
        last_point = last_item.end_point

        if isinstance(last_item, QuadraticBezierCurve):
            ctrl = (2 * last_item.end) - last_item.ctrl
        else:
            ctrl = last_point

        end = last_point + self.end

        return QuadraticBezierCurve(ctrl, end).render(
            path_gsds, style, last_item, initial_point
        )

    def render_debug(
        self, path_gsds, style, last_item, initial_point, debug_stream, pfx
    ):
        # pylint: disable=unused-argument
        rendered, resolved, initial_point = self.render(
            path_gsds, style, last_item, initial_point
        )
        debug_stream.write(f"{self} resolved to {resolved}\n")

        return rendered, resolved, initial_point


@force_nodocument
def svg_path_converter(pdf_path, svg_path):
    """Convert an SVG path string into a structured PDF path object"""
    if parse_path is None:
        raise EnvironmentError(
            "svg?path not available - fpdf2 cannot insert SVG images"
        )

    svg_path = svg_path.strip()
    if svg_path[0] not in {"M", "m"}:
        raise ValueError(f"SVG path does not start with moveto command: {svg_path}")

    current_pos = 0
    for cmd in parse_path(svg_path):
        if isinstance(cmd, Move):
            if cmd.relative:
                end = cmd.end - current_pos
                PaintedPath.move_relative(pdf_path, x=end.real, y=end.imag)
            else:
                PaintedPath.move_to(pdf_path, x=cmd.end.real, y=cmd.end.imag)
        elif isinstance(cmd, Line):
            if cmd.horizontal:
                if cmd.relative:
                    delta = cmd.end - current_pos
                    PaintedPath.horizontal_line_relative(pdf_path, dx=delta.real)
                else:
                    PaintedPath.horizontal_line_to(pdf_path, x=cmd.end.real)
            elif cmd.vertical:
                if cmd.relative:
                    delta = cmd.end - current_pos
                    PaintedPath.vertical_line_relative(pdf_path, dy=delta.imag)
                else:
                    PaintedPath.vertical_line_to(pdf_path, y=cmd.end.imag)
            else:
                if cmd.relative:
                    delta = cmd.end - current_pos
                    PaintedPath.line_relative(pdf_path, dx=delta.real, dy=delta.imag)
                else:
                    PaintedPath.line_to(pdf_path, x=cmd.end.real, y=cmd.end.imag)
        elif isinstance(cmd, Arc):
            if cmd.relative:
                end = cmd.end - current_pos
                PaintedPath.arc_relative(
                    pdf_path,
                    rx=cmd.radius.real,
                    ry=cmd.radius.imag,
                    rotation=cmd.rotation,
                    large_arc=cmd.arc,
                    positive_sweep=cmd.sweep,
                    dx=end.real,
                    dy=end.imag,
                )
            else:
                PaintedPath.arc_to(
                    pdf_path,
                    rx=cmd.radius.real,
                    ry=cmd.radius.imag,
                    rotation=cmd.rotation,
                    large_arc=cmd.arc,
                    positive_sweep=cmd.sweep,
                    x=cmd.end.real,
                    y=cmd.end.imag,
                )
        elif isinstance(cmd, CubicBezier):
            if cmd.smooth:
                if cmd.relative:
                    control2 = cmd.control2 - current_pos
                    end = cmd.end - current_pos
                    SVGRelativeSmoothCubicCurve.from_path_points(
                        pdf_path,
                        c2x=control2.real,
                        c2y=control2.imag,
                        ex=end.real,
                        ey=end.imag,
                    )
                else:
                    SVGSmoothCubicCurve.from_path_points(
                        pdf_path,
                        c2x=cmd.control2.real,
                        c2y=cmd.control2.imag,
                        ex=cmd.end.real,
                        ey=cmd.end.imag,
                    )
            else:
                if cmd.relative:
                    control1 = cmd.control1 - current_pos
                    control2 = cmd.control2 - current_pos
                    end = cmd.end - current_pos
                    PaintedPath.curve_relative(
                        pdf_path,
                        dx1=control1.real,
                        dy1=control1.imag,
                        dx2=control2.real,
                        dy2=control2.imag,
                        dx3=end.real,
                        dy3=end.imag,
                    )
                else:
                    PaintedPath.curve_to(
                        pdf_path,
                        x1=cmd.control1.real,
                        y1=cmd.control1.imag,
                        x2=cmd.control2.real,
                        y2=cmd.control2.imag,
                        x3=cmd.end.real,
                        y3=cmd.end.imag,
                    )
        elif isinstance(cmd, QuadraticBezier):
            if cmd.smooth:
                if cmd.relative:
                    end = cmd.end - current_pos
                    SVGRelativeSmoothQuadraticCurve.from_path_points(
                        pdf_path, ex=end.real, ey=end.imag
                    )
                else:
                    SVGSmoothQuadraticCurve.from_path_points(
                        pdf_path, ex=cmd.end.real, ey=cmd.end.imag
                    )
            else:
                if cmd.relative:
                    control = cmd.control - current_pos
                    end = cmd.end - current_pos
                    PaintedPath.quadratic_curve_relative(
                        pdf_path,
                        dx1=control.real,
                        dy1=control.imag,
                        dx2=end.real,
                        dy2=end.imag,
                    )
                else:
                    PaintedPath.quadratic_curve_to(
                        pdf_path,
                        x1=cmd.control.real,
                        y1=cmd.control.imag,
                        x2=cmd.end.real,
                        y2=cmd.end.imag,
                    )
        elif isinstance(cmd, Close):
            PaintedPath.close(pdf_path)
        else:
            raise NotImplementedError(f"Unsupported svg.path command type: {cmd}")
        current_pos = cmd.end


class SVGObject:
    """
    A representation of an SVG that has been converted to a PDF representation.
    """

    @classmethod
    def from_file(cls, filename, *args, encoding="utf-8", **kwargs):
        """
        Create an `SVGObject` from the contents of the file at `filename`.

        Args:
            filename (path-like): the path to a file containing SVG data.
            *args: forwarded directly to the SVGObject initializer. For subclass use.
            encoding (str): optional charset encoding to use when reading the file.
            **kwargs: forwarded directly to the SVGObject initializer. For subclass use.

        Returns:
            A converted `SVGObject`.
        """
        with open(filename, "r", encoding=encoding) as svgfile:
            return cls(svgfile.read(), *args, **kwargs)

    def __init__(self, svg_text):
        self.cross_references = {}

        svg_tree = parse_xml_str(svg_text)

        if svg_tree.tag not in xmlns_lookup("svg", "svg"):
            raise ValueError(f"root tag must be svg, not {svg_tree.tag}")

        self.extract_shape_info(svg_tree)
        self.convert_graphics(svg_tree)

    @force_nodocument
    def extract_shape_info(self, root_tag):
        """Collect shape info from the given SVG."""

        width = root_tag.get("width")
        height = root_tag.get("height")
        viewbox = root_tag.get("viewBox")
        # we don't fully support this, just check for its existence
        preserve_ar = root_tag.get("preserveAspectRatio", True)
        if preserve_ar == "none":
            self.preserve_ar = None
        else:
            self.preserve_ar = True

        self.width = None
        if width is not None:
            width.strip()
            if width.endswith("%"):
                self.width = Percent(width[:-1])
            else:
                self.width = resolve_length(width)

        self.height = None
        if height is not None:
            height.strip()
            if height.endswith("%"):
                self.height = Percent(height[:-1])
            else:
                self.height = resolve_length(height)

        if viewbox is None:
            self.viewbox = None
        else:
            viewbox.strip()
            vx, vy, vw, vh = [float(num) for num in NUMBER_SPLIT.split(viewbox)]
            if (vw < 0) or (vh < 0):
                raise ValueError(f"invalid negative width/height in viewbox {viewbox}")

            self.viewbox = [vx, vy, vw, vh]

    @force_nodocument
    def convert_graphics(self, root_tag):
        """Convert the graphics contained in the SVG into the PDF representation."""
        base_group = GraphicsContext()
        base_group.style.stroke_width = None
        base_group.style.auto_close = False
        base_group.style.stroke_cap_style = "butt"

        self.build_group(root_tag, base_group)

        self.base_group = base_group

    def transform_to_page_viewport(self, pdf, align_viewbox=True):
        """
        Size the converted SVG paths to the page viewport.

        The SVG document size can be specified relative to the rendering viewport
        (e.g. width=50%). If the converted SVG sizes are relative units, then this
        computes the appropriate scale transform to size the SVG to the correct
        dimensions for a page in the current PDF document.

        If the SVG document size is specified in absolute units, then it is not scaled.

        Args:
            pdf (fpdf.FPDF): the pdf to use the page size of.
            align_viewbox (bool): if True, mimic some of the SVG alignment rules if the
                viewbox aspect ratio does not match that of the viewport.

        Returns:
            The same thing as `SVGObject.transform_to_rect_viewport`.
        """

        return self.transform_to_rect_viewport(pdf.k, pdf.epw, pdf.eph, align_viewbox)

    def transform_to_rect_viewport(
        self, scale, width, height, align_viewbox=True, ignore_svg_top_attrs=False
    ):
        """
        Size the converted SVG paths to an arbitrarily sized viewport.

        The SVG document size can be specified relative to the rendering viewport
        (e.g. width=50%). If the converted SVG sizes are relative units, then this
        computes the appropriate scale transform to size the SVG to the correct
        dimensions for a page in the current PDF document.

        Args:
            scale (Number): the scale factor from document units to PDF points.
            width (Number): the width of the viewport to scale to in document units.
            height (Number): the height of the viewport to scale to in document units.
            align_viewbox (bool): if True, mimic some of the SVG alignment rules if the
                viewbox aspect ratio does not match that of the viewport.
            ignore_svg_top_attrs (bool): ignore <svg> top attributes like "width", "height"
                or "preserveAspectRatio" when figuring the image dimensions.
                Require width & height to be provided as parameters.

        Returns:
            A tuple of (width, height, `fpdf.drawing.GraphicsContext`), where width and
            height are the resolved width and height (they may be 0. If 0, the returned
            `fpdf.drawing.GraphicsContext` will be empty). The
            `fpdf.drawing.GraphicsContext` contains all of the paths that were
            converted from the SVG, scaled to the given viewport size.
        """

        if ignore_svg_top_attrs:
            vp_width = width
        elif isinstance(self.width, Percent):
            if not width:
                raise ValueError(
                    'SVG "width" is a percentage, hence a viewport width is required'
                )
            vp_width = self.width * width / 100
        else:
            vp_width = self.width or width

        if ignore_svg_top_attrs:
            vp_height = height
        elif isinstance(self.height, Percent):
            if not height:
                raise ValueError(
                    'SVG "height" is a percentage, hence a viewport height is required'
                )
            vp_height = self.height * height / 100
        else:
            vp_height = self.height or height

        if scale == 1:
            transform = Transform.identity()
        else:
            transform = Transform.scaling(1 / scale)

        if self.viewbox:
            vx, vy, vw, vh = self.viewbox

            if (vw == 0) or (vh == 0):
                return 0, 0, GraphicsContext()

            w_ratio = vp_width / vw
            h_ratio = vp_height / vh

            if not ignore_svg_top_attrs and self.preserve_ar and (w_ratio != h_ratio):
                w_ratio = h_ratio = min(w_ratio, h_ratio)

            transform = (
                transform
                @ Transform.translation(x=-vx, y=-vy)
                @ Transform.scaling(x=w_ratio, y=h_ratio)
            )

            if align_viewbox:
                transform = transform @ Transform.translation(
                    x=vp_width / 2 - (vw / 2) * w_ratio,
                    y=vp_height / 2 - (vh / 2) * h_ratio,
                )

        self.base_group.transform = transform

        return vp_width / scale, vp_height / scale, self.base_group

    def draw_to_page(self, pdf, x=None, y=None, debug_stream=None):
        """
        Directly draw the converted SVG to the given PDF's current page.

        The page viewport is used for sizing the SVG.

        Args:
            pdf (fpdf.FPDF): the document to which the converted SVG is rendered.
            x (Number): abscissa of the converted SVG's top-left corner.
            y (Number): ordinate of the converted SVG's top-left corner.
            debug_stream (io.TextIO): the stream to which rendering debug info will be
                written.
        """
        _, _, path = self.transform_to_page_viewport(pdf)

        old_x, old_y = pdf.x, pdf.y
        try:
            if x is not None and y is not None:
                pdf.set_xy(0, 0)
                path.transform = path.transform @ Transform.translation(x, y)

            pdf.draw_path(path, debug_stream)

        finally:
            pdf.set_xy(old_x, old_y)

    # defs paths are not drawn immediately but are added to xrefs and can be referenced
    # later to be drawn.
    @force_nodocument
    def handle_defs(self, defs):
        """Produce lookups for groups and paths inside the <defs> tag"""
        for child in defs:
            if child.tag in xmlns_lookup("svg", "g"):
                self.build_group(child)
            if child.tag in xmlns_lookup("svg", "path"):
                self.build_path(child)

    # this assumes xrefs only reference already-defined ids.
    # I don't know if this is required by the SVG spec.
    @force_nodocument
    def build_xref(self, xref):
        """Resolve a cross-reference to an already-seen SVG element by ID."""
        pdf_group = GraphicsContext()
        apply_styles(pdf_group, xref)

        for candidate in xmlns_lookup("xlink", "href"):
            try:
                ref = xref.attrib[candidate]
                break
            except KeyError:
                pass
        else:
            raise ValueError(f"use {xref} doesn't contain known xref attribute")

        try:
            pdf_group.add_item(self.cross_references[ref])
        except KeyError:
            raise ValueError(
                f"use {xref} references nonexistent ref id {ref}"
            ) from None

        if "x" in xref.attrib or "y" in xref.attrib:
            # Quoting the SVG spec - 5.6.2. Layout of re-used graphics:
            # > The x and y properties define an additional transformation translate(x,y)
            x, y = float(xref.attrib.get("x", 0)), float(xref.attrib.get("y", 0))
            pdf_group.transform = Transform.translation(x=x, y=y)
        # Note that we currently do not support "width" & "height" in <use>

        return pdf_group

    @force_nodocument
    def build_group(self, group, pdf_group=None):
        """Handle nested items within a group <g> tag."""
        if pdf_group is None:
            pdf_group = GraphicsContext()
            apply_styles(pdf_group, group)

        for child in group:
            if child.tag in xmlns_lookup("svg", "defs"):
                self.handle_defs(child)
            if child.tag in xmlns_lookup("svg", "g"):
                pdf_group.add_item(self.build_group(child))
            if child.tag in xmlns_lookup("svg", "path"):
                pdf_group.add_item(self.build_path(child))
            elif child.tag in shape_tags:
                pdf_group.add_item(getattr(ShapeBuilder, shape_tags[child.tag])(child))
            if child.tag in xmlns_lookup("svg", "use"):
                pdf_group.add_item(self.build_xref(child))

        try:
            self.cross_references["#" + group.attrib["id"]] = pdf_group
        except KeyError:
            pass

        return pdf_group

    @force_nodocument
    def build_path(self, path):
        """Convert an SVG <path> tag into a PDF path object."""
        pdf_path = PaintedPath()
        apply_styles(pdf_path, path)

        svg_path = path.attrib.get("d", None)

        if svg_path is not None:
            svg_path_converter(pdf_path, svg_path)

        try:
            self.cross_references["#" + path.attrib["id"]] = pdf_path
        except KeyError:
            pass

        return pdf_path

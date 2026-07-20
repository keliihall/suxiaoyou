"use client";

import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type CSSProperties,
} from "react";
import { useTranslation } from "react-i18next";
import {
  AlertTriangle,
  ChevronLeft,
  ChevronRight,
  Download,
  ExternalLink,
  FileWarning,
  Loader2,
  Maximize2,
  ZoomIn,
  ZoomOut,
} from "lucide-react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { ApiError, api, apiErrorMessage } from "@/lib/api";
import { base64ToBlob, downloadBlob } from "@/lib/browser-files";
import { API, IS_DESKTOP } from "@/lib/constants";
import { isRemoteMode } from "@/lib/remote-connection";
import { cn } from "@/lib/utils";
import { useWorkspaceStore } from "@/stores/workspace-store";

interface PptxRendererProps {
  filePath?: string;
}

interface PptxRun {
  text: string;
  family: string | null;
  size: number;
  bold: boolean;
  italic: boolean;
  underline: boolean;
  color: string;
}

interface PptxParagraph {
  runs: PptxRun[];
  align: "left" | "center" | "right" | "justify";
  level: number;
  bullet: boolean;
  spaceBefore: number;
  spaceAfter: number;
}

interface PptxTextFrame {
  paragraphs: PptxParagraph[];
  marginLeft: number;
  marginRight: number;
  marginTop: number;
  marginBottom: number;
  vertical: "top" | "middle" | "bottom";
}

interface PptxElementFrame {
  x: number;
  y: number;
  width: number;
  height: number;
  rotation: number;
}

interface PptxShapeElement extends PptxElementFrame {
  kind: "shape";
  geometry: string;
  fill: string;
  stroke: string;
  strokeWidth: number;
  flipH: boolean;
  flipV: boolean;
  arrowStart: boolean;
  arrowEnd: boolean;
  text?: PptxTextFrame;
}

interface PptxImageElement extends PptxElementFrame {
  kind: "image";
  assetId: string;
  cropLeft: number;
  cropRight: number;
  cropTop: number;
  cropBottom: number;
  flipH: boolean;
  flipV: boolean;
}

interface PptxTableCell {
  x: number;
  y: number;
  width: number;
  height: number;
  fill: string;
  text?: PptxTextFrame;
}

interface PptxTableElement extends PptxElementFrame {
  kind: "table";
  cells: PptxTableCell[];
}

interface PptxUnsupportedElement extends PptxElementFrame {
  kind: "unsupported";
  label: string;
}

type PptxElement =
  | PptxShapeElement
  | PptxImageElement
  | PptxTableElement
  | PptxUnsupportedElement;

interface PptxSlide {
  index: number;
  background: string;
  hidden: boolean;
  elements: PptxElement[];
}

interface PptxAsset {
  mimeType: string;
  dataUrl: string;
  width: number;
  height: number;
}

interface PptxPreviewResponse {
  name: string;
  path: string;
  width: number;
  height: number;
  slideCount: number;
  slides: PptxSlide[];
  assets: Record<string, PptxAsset>;
  warnings: string[];
  sceneNodeCount: number;
}

interface BinaryContentResponse {
  content_base64: string;
  name: string;
  mime_type: string;
}

const MIN_ZOOM = 0.5;
const MAX_ZOOM = 2.5;
const ZOOM_STEP = 0.25;
const POINT_TO_CSS_PIXEL = 96 / 72;
const SAFE_COLOR = /^#[0-9a-f]{6}$/i;
const SAFE_IMAGE_DATA_URL = /^data:image\/(?:png|jpeg|bmp);base64,[a-z0-9+/]+={0,2}$/i;
const PPTX_PREVIEW_DEDUP_WINDOW_MS = 2_000;
const MAX_PPTX_RESPONSE_SLIDES = 200;
const MAX_PPTX_RESPONSE_ASSETS = 64;
const MAX_PPTX_RESPONSE_SCENE_NODES = 25_000;
const MAX_PPTX_MAIN_DOM_NODES = 20_000;
const MAX_PPTX_THUMBNAIL_ELEMENTS = 48;
const PPTX_DETAILED_THUMBNAIL_RADIUS = 3;

const pptxPreviewRequests = new Map<
  string,
  { expiresAt: number | null; request: Promise<PptxPreviewResponse> }
>();

const WARNING_KEYS: Record<string, string> = {
  static_preview_limitations: "pptxStaticNotice",
  ignored_external_links: "pptxExternalLinksIgnored",
  ignored_embedded_content: "pptxEmbeddedContentIgnored",
  unsupported_embedded_content: "pptxEmbeddedContentIgnored",
  unsupported_image: "pptxUnsupportedImage",
  image_budget_exceeded: "pptxImageBudgetExceeded",
  total_image_pixels_exceeded: "pptxImageBudgetExceeded",
  asset_limit_exceeded: "pptxImageBudgetExceeded",
  text_truncated: "pptxTextTruncated",
  paragraph_limit_exceeded: "pptxSceneLimitExceeded",
  run_limit_exceeded: "pptxSceneLimitExceeded",
  scene_node_limit_exceeded: "pptxSceneLimitExceeded",
  scene_size_limit_exceeded: "pptxSceneLimitExceeded",
  dom_node_limit_exceeded: "pptxSceneLimitExceeded",
  shape_limit_exceeded: "pptxShapeLimitExceeded",
  table_cell_limit_exceeded: "pptxTableLimitExceeded",
  unsupported_group: "pptxGroupApproximated",
  shape_geometry_approximated: "pptxShapeApproximated",
  unsupported_element: "pptxUnsupportedElementWarning",
};

function basename(path: string): string {
  return path.split(/[\\/]/).pop() || "presentation.pptx";
}

function extension(path: string): string {
  const name = basename(path);
  const dot = name.lastIndexOf(".");
  return dot >= 0 ? name.slice(dot).toLowerCase() : "";
}

function finite(value: number, fallback = 0): number {
  return Number.isFinite(value) ? value : fallback;
}

function clamped(value: number, min: number, max: number): number {
  return Math.max(min, Math.min(max, finite(value, min)));
}

function safeColor(value: string, fallback: string): string {
  return SAFE_COLOR.test(value) ? value : fallback;
}

function safeFontFamily(value: string | null): string {
  if (!value) return "Arial, 'Microsoft YaHei', sans-serif";
  const cleaned = value.replace(/[^\p{L}\p{N} _.,-]/gu, "").slice(0, 120);
  return cleaned ? `${cleaned}, Arial, 'Microsoft YaHei', sans-serif` : "Arial, sans-serif";
}

function errorCode(error: unknown): string | null {
  if (!(error instanceof ApiError)) return null;
  const body = error.body;
  if (!body || typeof body !== "object" || !("detail" in body)) return null;
  const detail = (body as { detail: unknown }).detail;
  return typeof detail === "string" ? detail : null;
}

function loadPptxPreview(filePath: string, workspace: string | null) {
  const key = JSON.stringify([filePath, workspace]);
  const now = Date.now();
  const cached = pptxPreviewRequests.get(key);
  if (cached && (cached.expiresAt === null || cached.expiresAt > now)) {
    return cached.request;
  }

  const request = api.post<PptxPreviewResponse>(
    API.FILES.PPTX_PREVIEW,
    { path: filePath, workspace },
    { timeoutMs: 25_000 },
  );
  const entry = { expiresAt: null as number | null, request };
  pptxPreviewRequests.set(key, entry);
  void request.then(
    () => {
      entry.expiresAt = Date.now() + PPTX_PREVIEW_DEDUP_WINDOW_MS;
      setTimeout(() => {
        if (pptxPreviewRequests.get(key)?.request === request) {
          pptxPreviewRequests.delete(key);
        }
      }, PPTX_PREVIEW_DEDUP_WINDOW_MS);
    },
    () => {
      if (pptxPreviewRequests.get(key)?.request === request) {
        pptxPreviewRequests.delete(key);
      }
    },
  );
  return request;
}

function textFrameDomCost(frame?: PptxTextFrame): number {
  if (!frame) return 0;
  let cost = 1;
  for (const paragraph of frame.paragraphs) {
    cost += 1 + paragraph.runs.length + (paragraph.bullet ? 1 : 0);
  }
  return cost;
}

function elementDomCost(element: PptxElement): number {
  if (element.kind === "shape") {
    return 4 + (element.arrowStart || element.arrowEnd ? 3 : 0) + textFrameDomCost(element.text);
  }
  if (element.kind === "image") return 2;
  if (element.kind === "table") {
    return 1 + element.cells.reduce(
      (total, cell) => total + 1 + textFrameDomCost(cell.text),
      0,
    );
  }
  return 1;
}

function boundedSlideElements(
  elements: PptxElement[],
  maxDomNodes = MAX_PPTX_MAIN_DOM_NODES,
): { elements: PptxElement[]; truncated: boolean } {
  const bounded: PptxElement[] = [];
  let used = 2; // SlideCanvas wrapper and logical slide surface.
  let truncated = false;
  for (const element of elements) {
    const cost = elementDomCost(element);
    if (used + cost > maxDomNodes) {
      truncated = true;
      continue;
    }
    bounded.push(element);
    used += cost;
  }
  return { elements: bounded, truncated };
}

function boundPreviewResponse(response: PptxPreviewResponse): PptxPreviewResponse {
  const warnings = new Set(Array.isArray(response.warnings) ? response.warnings : []);
  const rawSlides = Array.isArray(response.slides) ? response.slides : [];
  const slides = rawSlides.slice(
    0,
    MAX_PPTX_RESPONSE_SLIDES,
  );
  if (slides.length !== rawSlides.length) {
    warnings.add("dom_node_limit_exceeded");
  }

  const rawAssets = response.assets && typeof response.assets === "object"
    ? response.assets
    : {};
  const assets = Object.fromEntries(
    Object.entries(rawAssets)
      .filter(([, asset]) => (
        asset
        && typeof asset === "object"
        && typeof asset.dataUrl === "string"
        && SAFE_IMAGE_DATA_URL.test(asset.dataUrl)
      ))
      .slice(0, MAX_PPTX_RESPONSE_ASSETS),
  );
  if (Object.keys(assets).length !== Object.keys(rawAssets).length) {
    warnings.add("dom_node_limit_exceeded");
  }

  if (
    !Number.isFinite(response.sceneNodeCount)
    || response.sceneNodeCount > MAX_PPTX_RESPONSE_SCENE_NODES
    || slides.some(
      (slide) => boundedSlideElements(slide.elements).truncated,
    )
  ) {
    warnings.add("dom_node_limit_exceeded");
  }

  return {
    ...response,
    slideCount: slides.length,
    slides,
    assets,
    warnings: Array.from(warnings),
    sceneNodeCount: clamped(
      response.sceneNodeCount,
      0,
      MAX_PPTX_RESPONSE_SCENE_NODES,
    ),
  };
}

function textJustification(vertical: PptxTextFrame["vertical"]): CSSProperties["justifyContent"] {
  if (vertical === "middle") return "center";
  if (vertical === "bottom") return "flex-end";
  return "flex-start";
}

function TextFrame({ frame }: { frame: PptxTextFrame }) {
  return (
    <div
      className="pointer-events-none absolute inset-0 flex min-h-0 flex-col overflow-hidden"
      style={{
        justifyContent: textJustification(frame.vertical),
        paddingLeft: clamped(frame.marginLeft, 0, 500),
        paddingRight: clamped(frame.marginRight, 0, 500),
        paddingTop: clamped(frame.marginTop, 0, 500),
        paddingBottom: clamped(frame.marginBottom, 0, 500),
      }}
    >
      {frame.paragraphs.map((paragraph, paragraphIndex) => (
        <p
          key={paragraphIndex}
          style={{
            margin: 0,
            marginTop: clamped(paragraph.spaceBefore, 0, 200) * POINT_TO_CSS_PIXEL,
            marginBottom: clamped(paragraph.spaceAfter, 0, 200) * POINT_TO_CSS_PIXEL,
            paddingLeft: clamped(paragraph.level, 0, 8) * 18,
            textAlign: paragraph.align,
            whiteSpace: "pre-wrap",
            overflowWrap: "anywhere",
            lineHeight: 1.08,
          }}
        >
          {paragraph.bullet && (
            <span aria-hidden="true" className="mr-[0.35em]">
              •
            </span>
          )}
          {paragraph.runs.map((run, runIndex) => (
            <span
              key={runIndex}
              style={{
                color: safeColor(run.color, "#1F1F1F"),
                fontFamily: safeFontFamily(run.family),
                fontSize: clamped(run.size, 4, 200) * POINT_TO_CSS_PIXEL,
                fontWeight: run.bold ? 700 : 400,
                fontStyle: run.italic ? "italic" : "normal",
                textDecoration: run.underline ? "underline" : "none",
              }}
            >
              {run.text}
            </span>
          ))}
        </p>
      ))}
    </div>
  );
}

function frameStyle(frame: PptxElementFrame, flipH = false, flipV = false): CSSProperties {
  const transforms = [
    `rotate(${clamped(frame.rotation, -3600, 3600)}deg)`,
    flipH ? "scaleX(-1)" : "",
    flipV ? "scaleY(-1)" : "",
  ].filter(Boolean);
  return {
    position: "absolute",
    left: finite(frame.x),
    top: finite(frame.y),
    width: Math.max(0, finite(frame.width)),
    height: Math.max(0, finite(frame.height)),
    transform: transforms.join(" ") || undefined,
    transformOrigin: "center",
  };
}

function polygonPoints(geometry: string, width: number, height: number): string | null {
  const w = Math.max(1, width);
  const h = Math.max(1, height);
  const points: Record<string, Array<[number, number]>> = {
    triangle: [[w / 2, 0], [w, h], [0, h]],
    rightTriangle: [[0, 0], [w, h], [0, h]],
    diamond: [[w / 2, 0], [w, h / 2], [w / 2, h], [0, h / 2]],
    pentagon: [[w / 2, 0], [w, h * 0.38], [w * 0.82, h], [w * 0.18, h], [0, h * 0.38]],
    hexagon: [[w * 0.25, 0], [w * 0.75, 0], [w, h / 2], [w * 0.75, h], [w * 0.25, h], [0, h / 2]],
    chevron: [[0, 0], [w * 0.65, 0], [w, h / 2], [w * 0.65, h], [0, h], [w * 0.35, h / 2]],
    parallelogram: [[w * 0.2, 0], [w, 0], [w * 0.8, h], [0, h]],
    trapezoid: [[w * 0.2, 0], [w * 0.8, 0], [w, h], [0, h]],
    rightArrow: [[0, h * 0.25], [w * 0.62, h * 0.25], [w * 0.62, 0], [w, h / 2], [w * 0.62, h], [w * 0.62, h * 0.75], [0, h * 0.75]],
    leftArrow: [[w, h * 0.25], [w * 0.38, h * 0.25], [w * 0.38, 0], [0, h / 2], [w * 0.38, h], [w * 0.38, h * 0.75], [w, h * 0.75]],
    upArrow: [[w * 0.25, h], [w * 0.25, h * 0.38], [0, h * 0.38], [w / 2, 0], [w, h * 0.38], [w * 0.75, h * 0.38], [w * 0.75, h]],
    downArrow: [[w * 0.25, 0], [w * 0.25, h * 0.62], [0, h * 0.62], [w / 2, h], [w, h * 0.62], [w * 0.75, h * 0.62], [w * 0.75, 0]],
  };
  const geometryPoints = points[geometry];
  return geometryPoints?.map(([x, y]) => `${x},${y}`).join(" ") ?? null;
}

function ShapeElement({ element, markerId }: { element: PptxShapeElement; markerId: string }) {
  const width = Math.max(1, finite(element.width, 1));
  const height = Math.max(1, finite(element.height, 1));
  const fill = safeColor(element.fill, "transparent");
  const stroke = safeColor(element.stroke, "transparent");
  const strokeWidth = clamped(element.strokeWidth, 0, 50);
  const points = polygonPoints(element.geometry, width, height);

  let graphic: React.ReactNode;
  if (element.geometry === "line") {
    graphic = (
      <line
        x1="0"
        y1="0"
        x2={width}
        y2={height}
        stroke={stroke}
        strokeWidth={Math.max(1, strokeWidth)}
        markerStart={element.arrowStart ? `url(#${markerId})` : undefined}
        markerEnd={element.arrowEnd ? `url(#${markerId})` : undefined}
      />
    );
  } else if (element.geometry === "ellipse") {
    graphic = <ellipse cx={width / 2} cy={height / 2} rx={width / 2} ry={height / 2} fill={fill} stroke={stroke} strokeWidth={strokeWidth} />;
  } else if (points) {
    graphic = <polygon points={points} fill={fill} stroke={stroke} strokeWidth={strokeWidth} />;
  } else {
    graphic = (
      <rect
        x={strokeWidth / 2}
        y={strokeWidth / 2}
        width={Math.max(0, width - strokeWidth)}
        height={Math.max(0, height - strokeWidth)}
        rx={element.geometry === "roundRect" ? Math.min(width, height) * 0.12 : 0}
        fill={fill}
        stroke={stroke}
        strokeWidth={strokeWidth}
      />
    );
  }

  return (
    <div style={frameStyle(element, element.flipH, element.flipV)}>
      <svg aria-hidden="true" className="absolute inset-0 h-full w-full overflow-visible" viewBox={`0 0 ${width} ${height}`} preserveAspectRatio="none">
        {(element.arrowStart || element.arrowEnd) && (
          <defs>
            <marker id={markerId} markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto-start-reverse" markerUnits="strokeWidth">
              <path d="M0,0 L8,4 L0,8 Z" fill={stroke} />
            </marker>
          </defs>
        )}
        {graphic}
      </svg>
      {element.text && <TextFrame frame={element.text} />}
    </div>
  );
}

function ImageElement({ element, asset }: { element: PptxImageElement; asset?: PptxAsset }) {
  const source = asset && SAFE_IMAGE_DATA_URL.test(asset.dataUrl) ? asset.dataUrl : null;
  const horizontalVisible = Math.max(0.01, 1 - element.cropLeft - element.cropRight);
  const verticalVisible = Math.max(0.01, 1 - element.cropTop - element.cropBottom);
  return (
    <div className="overflow-hidden" style={frameStyle(element, element.flipH, element.flipV)}>
      {source && (
        // The backend allow-lists both MIME type and file signature. SVG and
        // animated formats are never returned, so this data URL is inert.
        // eslint-disable-next-line @next/next/no-img-element
        <img
          src={source}
          alt=""
          draggable={false}
          className="pointer-events-none absolute max-w-none select-none"
          style={{
            left: `${(-element.cropLeft / horizontalVisible) * 100}%`,
            top: `${(-element.cropTop / verticalVisible) * 100}%`,
            width: `${100 / horizontalVisible}%`,
            height: `${100 / verticalVisible}%`,
          }}
        />
      )}
    </div>
  );
}

function TableElement({ element }: { element: PptxTableElement }) {
  return (
    <div style={frameStyle(element)}>
      {element.cells.map((cell, index) => (
        <div
          key={index}
          className="absolute overflow-hidden border border-black/20"
          style={{
            left: finite(cell.x),
            top: finite(cell.y),
            width: Math.max(0, finite(cell.width)),
            height: Math.max(0, finite(cell.height)),
            background: safeColor(cell.fill, "#FFFFFF"),
          }}
        >
          {cell.text && <TextFrame frame={cell.text} />}
        </div>
      ))}
    </div>
  );
}

function UnsupportedElement({ element }: { element: PptxUnsupportedElement }) {
  const { t } = useTranslation("chat");
  return (
    <div
      style={frameStyle(element)}
      className="flex items-center justify-center overflow-hidden border border-dashed border-amber-600/60 bg-amber-100/50 px-1 text-center text-[10px] text-amber-900"
      title={element.label}
    >
      {t("pptxUnsupportedElement")}
    </div>
  );
}

function ThumbnailCanvas({
  slide,
  width,
  height,
  scale,
  assets,
  detailed,
}: {
  slide: PptxSlide;
  width: number;
  height: number;
  scale: number;
  assets: Record<string, PptxAsset>;
  detailed: boolean;
}) {
  const safeWidth = clamped(width, 1, 10_000);
  const safeHeight = clamped(height, 1, 10_000);
  const safeScale = clamped(scale, 0.01, 10);
  const elements = detailed
    ? slide.elements.slice(0, MAX_PPTX_THUMBNAIL_ELEMENTS)
    : [];
  return (
    <div
      className="relative shrink-0"
      style={{ width: safeWidth * safeScale, height: safeHeight * safeScale }}
    >
      <div
        className="absolute left-0 top-0 origin-top-left overflow-hidden"
        style={{
          width: safeWidth,
          height: safeHeight,
          background: safeColor(slide.background, "#FFFFFF"),
          transform: `scale(${safeScale})`,
        }}
      >
        {elements.map((element, index) => {
          if (element.kind === "image") {
            const asset = assets[element.assetId];
            const source = asset && SAFE_IMAGE_DATA_URL.test(asset.dataUrl)
              ? asset.dataUrl
              : null;
            return source ? (
              // eslint-disable-next-line @next/next/no-img-element
              <img
                key={index}
                src={source}
                alt=""
                draggable={false}
                className="absolute"
                style={frameStyle(element, element.flipH, element.flipV)}
              />
            ) : null;
          }
          const fill = element.kind === "shape"
            ? safeColor(element.fill, "transparent")
            : element.kind === "table"
              ? "#FFFFFF"
              : "#FEF3C7";
          return (
            <div
              key={index}
              className="absolute border border-black/10"
              style={{
                ...frameStyle(element),
                background: fill,
                borderRadius:
                  element.kind === "shape" && element.geometry === "ellipse"
                    ? "50%"
                    : 0,
              }}
            />
          );
        })}
      </div>
    </div>
  );
}

function SlideCanvas({
  slide,
  width,
  height,
  scale,
  assets,
  className,
}: {
  slide: PptxSlide;
  width: number;
  height: number;
  scale: number;
  assets: Record<string, PptxAsset>;
  className?: string;
}) {
  const safeWidth = clamped(width, 1, 10_000);
  const safeHeight = clamped(height, 1, 10_000);
  const safeScale = clamped(scale, 0.01, 10);
  const bounded = useMemo(
    () => boundedSlideElements(slide.elements),
    [slide.elements],
  );
  return (
    <div
      className={cn("relative shrink-0", className)}
      style={{ width: safeWidth * safeScale, height: safeHeight * safeScale }}
    >
      <div
        className="absolute left-0 top-0 origin-top-left overflow-hidden shadow-sm"
        style={{
          width: safeWidth,
          height: safeHeight,
          background: safeColor(slide.background, "#FFFFFF"),
          transform: `scale(${safeScale})`,
        }}
      >
        {bounded.elements.map((element, index) => {
          if (element.kind === "shape") {
            return <ShapeElement key={index} element={element} markerId={`pptx-arrow-${slide.index}-${index}`} />;
          }
          if (element.kind === "image") {
            return <ImageElement key={index} element={element} asset={assets[element.assetId]} />;
          }
          if (element.kind === "table") {
            return <TableElement key={index} element={element} />;
          }
          return <UnsupportedElement key={index} element={element} />;
        })}
      </div>
    </div>
  );
}

function Fallback({
  title,
  message,
  onDownload,
  onOpenExternal,
  downloading,
}: {
  title: string;
  message: string;
  onDownload: () => void;
  onOpenExternal?: () => void;
  downloading: boolean;
}) {
  const { t } = useTranslation("chat");
  return (
    <div className="flex flex-1 items-center justify-center p-6">
      <div className="max-w-md text-center">
        <FileWarning className="mx-auto mb-3 h-8 w-8 text-[var(--text-tertiary)]" />
        <p className="text-sm font-medium text-[var(--text-primary)]">{title}</p>
        <p className="mt-1 text-xs leading-5 text-[var(--text-secondary)]">{message}</p>
        <div className="mt-4 flex flex-wrap justify-center gap-2">
          <Button onClick={onDownload} disabled={downloading}>
            {downloading ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : <Download className="mr-2 h-4 w-4" />}
            {t("pptxDownloadOriginal")}
          </Button>
          {onOpenExternal && (
            <Button variant="outline" onClick={onOpenExternal}>
              <ExternalLink className="mr-2 h-4 w-4" />
              {t("pptxOpenExternallyAction")}
            </Button>
          )}
        </div>
      </div>
    </div>
  );
}

export function PptxRenderer({ filePath }: PptxRendererProps) {
  const { t } = useTranslation("chat");
  const workspace = useWorkspaceStore((state) => state.activeWorkspacePath);
  const viewportRef = useRef<HTMLDivElement>(null);
  const [preview, setPreview] = useState<PptxPreviewResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [fileName, setFileName] = useState(filePath ? basename(filePath) : "presentation.pptx");
  const [activeSlide, setActiveSlide] = useState(0);
  const [zoom, setZoom] = useState(1);
  const [viewportSize, setViewportSize] = useState({ width: 0, height: 0 });
  const [downloading, setDownloading] = useState(false);
  const localDesktop = IS_DESKTOP && !isRemoteMode();
  const isLegacyPpt = Boolean(filePath && extension(filePath) === ".ppt");

  useEffect(() => {
    setFileName(filePath ? basename(filePath) : "presentation.pptx");
    setPreview(null);
    setActiveSlide(0);
    setZoom(1);

    if (!filePath) {
      setError(t("pptxMissingPath"));
      setLoading(false);
      return;
    }
    if (isLegacyPpt) {
      setError(t("pptxLegacyUnsupported"));
      setLoading(false);
      return;
    }

    let cancelled = false;
    void (async () => {
      try {
        setLoading(true);
        setError(null);
        const response = boundPreviewResponse(
          await loadPptxPreview(filePath, workspace),
        );
        if (cancelled) return;
        if (!response.slides.length || response.slideCount !== response.slides.length) {
          throw new Error("invalid_pptx_preview_response");
        }
        setPreview(response);
        setFileName(response.name || basename(filePath));
      } catch (cause) {
        if (cancelled) return;
        const code = errorCode(cause);
        const key =
          code === "ppt_legacy_unsupported"
            ? "pptxLegacyUnsupported"
            : code === "pptx_preview_busy"
              ? "pptxPreviewBusy"
            : code === "pptx_file_size_limit" || code?.includes("_limit")
              ? "pptxLimitExceeded"
              : code === "pptx_preview_timeout"
                ? "pptxPreviewTimedOut"
                : code?.startsWith("pptx_invalid") || code === "pptx_parse_failed"
                  ? "pptxInvalidFile"
                  : null;
        setError(key ? t(key) : apiErrorMessage(cause, t("pptxLoadFailed")));
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();

    return () => {
      cancelled = true;
    };
  }, [filePath, isLegacyPpt, t, workspace]);

  useEffect(() => {
    const viewport = viewportRef.current;
    if (!viewport) return;
    const update = () => {
      const rect = viewport.getBoundingClientRect();
      setViewportSize({ width: rect.width, height: rect.height });
    };
    update();
    const observer = new ResizeObserver(update);
    observer.observe(viewport);
    return () => observer.disconnect();
  }, [preview]);

  const handleDownload = useCallback(async () => {
    if (!filePath || downloading) return;
    setDownloading(true);
    try {
      const response = await api.post<BinaryContentResponse>(
        API.FILES.CONTENT_BINARY,
        { path: filePath, workspace },
        { timeoutMs: 120_000 },
      );
      downloadBlob(
        base64ToBlob(response.content_base64, response.mime_type),
        response.name || fileName,
      );
    } catch (cause) {
      console.error("Failed to download PPTX:", cause);
      const tooLarge = cause instanceof ApiError && cause.status === 413;
      toast.error(t(tooLarge ? "pptxDownloadLimitExceeded" : "fileSaveFailed"));
    } finally {
      setDownloading(false);
    }
  }, [downloading, fileName, filePath, t, workspace]);

  const handleOpenExternal = useCallback(async () => {
    if (!filePath || !localDesktop) return;
    try {
      await api.post(API.FILES.OPEN_SYSTEM, { path: filePath, workspace });
    } catch (cause) {
      console.error("Failed to open presentation externally:", cause);
      toast.error(t("fileOpenFailed"));
    }
  }, [filePath, localDesktop, t, workspace]);

  const fitScale = useMemo(() => {
    if (!preview || viewportSize.width <= 0 || viewportSize.height <= 0) return 0.5;
    return Math.max(
      0.05,
      Math.min(
        (viewportSize.width - 32) / preview.width,
        (viewportSize.height - 32) / preview.height,
      ),
    );
  }, [preview, viewportSize]);

  const warningMessages = useMemo(() => {
    if (!preview) return [];
    return Array.from(
      new Set(
        preview.warnings.map((warning) =>
          t(WARNING_KEYS[warning] || "pptxUnsupportedElementWarning"),
        ),
      ),
    );
  }, [preview, t]);

  const goToSlide = useCallback((index: number) => {
    if (!preview) return;
    setActiveSlide(clamped(index, 0, preview.slides.length - 1));
  }, [preview]);

  if (loading) {
    return (
      <div className="flex h-full items-center justify-center">
        <div className="text-center">
          <Loader2 className="mx-auto h-5 w-5 animate-spin text-[var(--text-tertiary)]" />
          <p className="mt-2 text-xs text-[var(--text-tertiary)]">{t("pptxRendering")}</p>
        </div>
      </div>
    );
  }

  if (error || !preview) {
    return (
      <div className="flex h-full flex-col">
        <Fallback
          title={isLegacyPpt ? t("pptxLegacyTitle") : t("pptxPreviewUnavailable")}
          message={error || t("pptxOpenExternally")}
          onDownload={() => void handleDownload()}
          onOpenExternal={localDesktop ? () => void handleOpenExternal() : undefined}
          downloading={downloading}
        />
      </div>
    );
  }

  const slide = preview.slides[activeSlide];
  const thumbnailScale = Math.min(96 / preview.width, 64 / preview.height);
  const slideScale = fitScale * zoom;

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="flex shrink-0 items-center justify-between gap-2 border-b border-[var(--border-default)] bg-[var(--surface-tertiary)] px-2 py-1.5">
        <span className="min-w-0 truncate text-[11px] font-medium text-[var(--text-secondary)]" title={fileName}>
          {fileName}
        </span>
        <div className="flex shrink-0 items-center gap-0.5">
          <Button variant="ghost" size="icon" className="h-7 w-7" onClick={() => goToSlide(activeSlide - 1)} disabled={activeSlide === 0} title={t("pptxPreviousSlide")}>
            <ChevronLeft className="h-3.5 w-3.5" />
          </Button>
          <span className="min-w-[3.5rem] text-center text-[11px] tabular-nums text-[var(--text-secondary)]">
            {t("pptxSlidePosition", { current: activeSlide + 1, total: preview.slideCount })}
          </span>
          <Button variant="ghost" size="icon" className="h-7 w-7" onClick={() => goToSlide(activeSlide + 1)} disabled={activeSlide >= preview.slides.length - 1} title={t("pptxNextSlide")}>
            <ChevronRight className="h-3.5 w-3.5" />
          </Button>
          <span className="mx-1 h-4 w-px bg-[var(--border-default)]" />
          <Button variant="ghost" size="icon" className="h-7 w-7" onClick={() => setZoom((value) => clamped(value - ZOOM_STEP, MIN_ZOOM, MAX_ZOOM))} disabled={zoom <= MIN_ZOOM} title={t("pptxZoomOut")}>
            <ZoomOut className="h-3.5 w-3.5" />
          </Button>
          <span className="min-w-9 text-center text-[10px] tabular-nums text-[var(--text-tertiary)]">{Math.round(zoom * 100)}%</span>
          <Button variant="ghost" size="icon" className="h-7 w-7" onClick={() => setZoom((value) => clamped(value + ZOOM_STEP, MIN_ZOOM, MAX_ZOOM))} disabled={zoom >= MAX_ZOOM} title={t("pptxZoomIn")}>
            <ZoomIn className="h-3.5 w-3.5" />
          </Button>
          <Button variant="ghost" size="icon" className="h-7 w-7" onClick={() => setZoom(1)} disabled={zoom === 1} title={t("pptxFitToWindow")}>
            <Maximize2 className="h-3.5 w-3.5" />
          </Button>
          {localDesktop && (
            <Button variant="ghost" size="icon" className="h-7 w-7" onClick={() => void handleOpenExternal()} title={t("pptxOpenExternallyAction")}>
              <ExternalLink className="h-3.5 w-3.5" />
            </Button>
          )}
          <Button variant="ghost" size="icon" className="h-7 w-7" onClick={() => void handleDownload()} disabled={downloading} title={t("pptxDownloadOriginal")}>
            {downloading ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Download className="h-3.5 w-3.5" />}
          </Button>
        </div>
      </div>

      <div className="flex min-h-0 min-w-0 flex-1">
        {preview.slides.length > 1 && (
          <aside className="w-28 shrink-0 overflow-y-auto border-r border-[var(--border-default)] bg-[var(--surface-secondary)] p-2" aria-label={t("pptxThumbnails")}>
            <div className="flex flex-col gap-2">
              {preview.slides.map((thumbnail, index) => (
                <button
                  key={thumbnail.index}
                  type="button"
                  onClick={() => goToSlide(index)}
                  className={cn(
                    "rounded-md border p-1 text-left transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--brand-primary)]",
                    index === activeSlide
                      ? "border-[var(--brand-primary)] bg-[var(--brand-primary)]/10"
                      : "border-[var(--border-default)] hover:border-[var(--border-emphasis)]",
                  )}
                  aria-label={t("pptxGoToSlide", { slide: index + 1 })}
                  aria-current={index === activeSlide ? "page" : undefined}
                >
                  <div className="flex justify-center overflow-hidden bg-[var(--surface-tertiary)]">
                    <ThumbnailCanvas
                      slide={thumbnail}
                      width={preview.width}
                      height={preview.height}
                      scale={thumbnailScale}
                      assets={preview.assets}
                      detailed={
                        Math.abs(index - activeSlide)
                        <= PPTX_DETAILED_THUMBNAIL_RADIUS
                      }
                    />
                  </div>
                  <div className="mt-1 flex items-center justify-center gap-1 text-[10px] tabular-nums text-[var(--text-tertiary)]">
                    {index + 1}
                    {thumbnail.hidden && <span>{t("pptxHiddenSlide")}</span>}
                  </div>
                </button>
              ))}
            </div>
          </aside>
        )}

        <div
          ref={viewportRef}
          className="min-w-0 flex-1 overflow-auto bg-[var(--surface-secondary)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-[var(--brand-primary)]"
          tabIndex={0}
          aria-label={t("pptxSlidePreview", { slide: activeSlide + 1 })}
          onKeyDown={(event) => {
            if (event.key === "ArrowLeft" || event.key === "PageUp") {
              event.preventDefault();
              goToSlide(activeSlide - 1);
            } else if (event.key === "ArrowRight" || event.key === "PageDown") {
              event.preventDefault();
              goToSlide(activeSlide + 1);
            }
          }}
        >
          <div className="flex min-h-full min-w-full items-center justify-center p-4">
            <SlideCanvas slide={slide} width={preview.width} height={preview.height} scale={slideScale} assets={preview.assets} className="ring-1 ring-black/10" />
          </div>
        </div>
      </div>

      {warningMessages.length > 0 && (
        <div className="flex shrink-0 items-start gap-2 border-t border-amber-500/20 bg-amber-500/10 px-3 py-2 text-[11px] leading-4 text-[var(--text-secondary)]" role="note">
          <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0 text-amber-600" />
          <span>{warningMessages.join(" ")}</span>
        </div>
      )}
    </div>
  );
}

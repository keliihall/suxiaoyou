"use client";

import { useEffect, useState } from "react";
import { ZoomIn, ZoomOut, Maximize } from "lucide-react";
import Image from "next/image";
import { Button } from "@/components/ui/button";

interface SvgRendererProps {
  content: string;
}

export function SvgRenderer({ content }: SvgRendererProps) {
  const [zoom, setZoom] = useState(100);
  const [svgDataUrl, setSvgDataUrl] = useState("");

  useEffect(() => {
    const blob = new Blob([content], { type: "image/svg+xml" });
    const objectUrl = URL.createObjectURL(blob);
    setSvgDataUrl(objectUrl);

    return () => URL.revokeObjectURL(objectUrl);
  }, [content]);

  return (
    <div className="flex flex-col h-full">
      {/* Toolbar */}
      <div className="flex items-center justify-between px-3 py-2 border-b border-[var(--border-default)] bg-[var(--surface-tertiary)] shrink-0">
        <span className="text-[11px] font-medium text-[var(--text-secondary)]">
          {zoom}%
        </span>
        <div className="flex items-center gap-1">
          <Button
            variant="ghost"
            size="icon"
            className="h-7 w-7"
            onClick={() => setZoom(Math.max(25, zoom - 25))}
          >
            <ZoomOut className="h-3.5 w-3.5" />
          </Button>
          <Button
            variant="ghost"
            size="icon"
            className="h-7 w-7"
            onClick={() => setZoom(100)}
          >
            <Maximize className="h-3.5 w-3.5" />
          </Button>
          <Button
            variant="ghost"
            size="icon"
            className="h-7 w-7"
            onClick={() => setZoom(Math.min(400, zoom + 25))}
          >
            <ZoomIn className="h-3.5 w-3.5" />
          </Button>
        </div>
      </div>

      {/* SVG display */}
      <div className="flex-1 overflow-auto flex items-center justify-center bg-[var(--surface-secondary)] p-4">
        {svgDataUrl && (
          <Image
            src={svgDataUrl}
            alt="SVG Preview"
            width={800}
            height={600}
            unoptimized
            style={{ transform: `scale(${zoom / 100})`, transformOrigin: "center center" }}
            className="max-w-full transition-transform"
          />
        )}
      </div>
    </div>
  );
}

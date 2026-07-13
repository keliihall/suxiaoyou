"use client";

import { useTranslation } from "react-i18next";

interface SuxiaoyouLogoProps {
  size?: number;
  className?: string;
}

export function SuxiaoyouLogo({ size = 20, className }: SuxiaoyouLogoProps) {
  const { t } = useTranslation("common");
  return (
    // eslint-disable-next-line @next/next/no-img-element
    <img
      src="/favicon.svg"
      width={size}
      height={size}
      alt={t("appName")}
      className={className}
      style={{ width: size, height: size }}
    />
  );
}

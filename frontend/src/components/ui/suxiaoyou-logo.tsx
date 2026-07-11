interface SuxiaoyouLogoProps {
  size?: number;
  className?: string;
}

export function SuxiaoyouLogo({ size = 20, className }: SuxiaoyouLogoProps) {
  return (
    // eslint-disable-next-line @next/next/no-img-element
    <img
      src="/favicon.svg"
      width={size}
      height={size}
      alt="苏小有"
      className={className}
      style={{ width: size, height: size }}
    />
  );
}

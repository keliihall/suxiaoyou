export const LATEST_RELEASE_URL =
  "https://github.com/keliihall/suxiaoyou/releases/latest";

interface ReleasePageOpeners {
  desktop: boolean;
  openDesktop: (url: string) => Promise<void>;
  openWeb: (url: string, target: string, features: string) => unknown;
}

/** Open the official release page; never downloads or installs implicitly. */
export async function openLatestReleasePage({
  desktop,
  openDesktop,
  openWeb,
}: ReleasePageOpeners): Promise<void> {
  if (desktop) {
    await openDesktop(LATEST_RELEASE_URL);
    return;
  }
  openWeb(LATEST_RELEASE_URL, "_blank", "noopener,noreferrer");
}

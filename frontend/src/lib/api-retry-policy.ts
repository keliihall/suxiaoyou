/** Max retries for transient network failures on explicitly safe requests. */
export const NETWORK_RETRY_MAX = 3;

/**
 * Return the number of network retries permitted for an HTTP method.
 *
 * Mutating methods are deliberately not retried unless the caller explicitly
 * opts in after supplying an endpoint-level idempotency key.
 */
export function networkRetryLimit(
  method: string,
  retryNetworkErrors?: boolean,
): number {
  if (retryNetworkErrors !== undefined) {
    return retryNetworkErrors ? NETWORK_RETRY_MAX : 0;
  }
  const normalized = method.toUpperCase();
  return normalized === "GET" || normalized === "HEAD" ? NETWORK_RETRY_MAX : 0;
}

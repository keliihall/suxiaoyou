/**
 * A monotonically increasing lease for the stream a session expects to own.
 *
 * `startStream` has asynchronous setup (desktop URL/token discovery). A lease
 * lets every continuation prove it still belongs to the newest request before
 * attaching a client or mutating session state.
 */
export interface StreamLease {
  sessionId: string;
  streamId: string;
  generation: number;
}

export class StreamLeaseRegistry {
  private nextGeneration = 0;
  private expected = new Map<string, StreamLease>();

  expect(sessionId: string, streamId: string): StreamLease {
    const lease = {
      sessionId,
      streamId,
      generation: ++this.nextGeneration,
    };
    this.expected.set(sessionId, lease);
    return lease;
  }

  isCurrent(lease: StreamLease): boolean {
    return this.expected.get(lease.sessionId) === lease;
  }

  current(sessionId: string): StreamLease | undefined {
    return this.expected.get(sessionId);
  }

  /**
   * Clear an expectation. Supplying a lease makes the operation conditional,
   * so cleanup from an old stream cannot invalidate a newer one.
   */
  clear(sessionId: string, lease?: StreamLease): boolean {
    if (lease && !this.isCurrent(lease)) return false;
    return this.expected.delete(sessionId);
  }

  clearAll(): void {
    this.expected.clear();
  }
}

/** A session-level boolean is insufficient when the backend replaces its job. */
export function needsRemoteStreamAttach(
  backendStreamId: string,
  activeStreamId: string | null,
): boolean {
  return backendStreamId !== activeStreamId;
}

export interface RemoteAttachSnapshot {
  registryStreamId: string | null;
  registryGeneration: number | null;
  bucketStreamId: string | null;
  bucketGenerationStartedAt: number | null;
}

/**
 * Commit an async poll result only if both the backend and every local owner
 * are still exactly the state observed before the await.
 */
export function canCommitRemoteStreamAttach(args: {
  pollSequence: number;
  currentPollSequence: number;
  expectedBackendStreamId: string;
  confirmedBackendStreamId: string | null;
  before: RemoteAttachSnapshot;
  after: RemoteAttachSnapshot;
}): boolean {
  const { before, after } = args;
  return args.pollSequence === args.currentPollSequence
    && args.confirmedBackendStreamId === args.expectedBackendStreamId
    && before.registryStreamId === after.registryStreamId
    && before.registryGeneration === after.registryGeneration
    && before.bucketStreamId === after.bucketStreamId
    && before.bucketGenerationStartedAt === after.bucketGenerationStartedAt;
}

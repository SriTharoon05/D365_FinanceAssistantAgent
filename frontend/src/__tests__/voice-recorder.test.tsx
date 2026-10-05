import { act, renderHook } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { useRecorder } from '../hooks/useRecorder';

describe('microphone lifecycle', () => {
  it('returns a useful browser support error without breaking text chat', async () => {
    vi.stubGlobal('MediaRecorder', undefined);
    const onTranscript = vi.fn();
    const onError = vi.fn();
    const { result } = renderHook(() => useRecorder(onTranscript, onError));

    await act(async () => result.current.start());

    expect(onError).toHaveBeenCalledWith(
      'Microphone recording requires a supported browser and HTTPS (or localhost).',
    );
    expect(result.current.state).toBe('idle');
    expect(onTranscript).not.toHaveBeenCalled();
  });

  it('releases a microphone permission request that resolves after recording was cancelled', async () => {
    let allowMicrophone!: (stream: MediaStream) => void;
    const permission = new Promise<MediaStream>((resolve) => {
      allowMicrophone = resolve;
    });
    const stop = vi.fn();
    vi.stubGlobal('MediaRecorder', class {});
    vi.stubGlobal('navigator', {
      mediaDevices: { getUserMedia: vi.fn().mockReturnValue(permission) },
    });
    const onTranscript = vi.fn();
    const onError = vi.fn();
    const { result } = renderHook(() => useRecorder(onTranscript, onError));
    let request!: Promise<void>;
    act(() => {
      request = result.current.start();
    });
    act(() => result.current.cancel());

    await act(async () => {
      allowMicrophone({ getTracks: () => [{ stop }] } as unknown as MediaStream);
      await request;
    });

    expect(stop).toHaveBeenCalledTimes(1);
    expect(result.current.state).toBe('idle');
    expect(onTranscript).not.toHaveBeenCalled();
    expect(onError).not.toHaveBeenCalled();
  });

  it('explains denied microphone permission and keeps the recorder idle', async () => {
    vi.stubGlobal('MediaRecorder', class {});
    vi.stubGlobal('navigator', {
      mediaDevices: {
        getUserMedia: vi
          .fn()
          .mockRejectedValue(new DOMException('Permission denied', 'NotAllowedError')),
      },
    });
    const onError = vi.fn();
    const { result } = renderHook(() => useRecorder(vi.fn(), onError));

    await act(async () => result.current.start());

    expect(onError).toHaveBeenCalledWith(
      'Microphone access was denied. Allow microphone access in your browser to record.',
    );
    expect(result.current.state).toBe('idle');
  });
});

import { useEffect, useRef, useState } from 'react';
import { endpoints } from '../lib/api';
export function useRecorder(
  onTranscript: (text: string) => void,
  onError: (message: string) => void,
  maxDuration = 120,
  maxUploadMb = 20,
) {
  const [state, setState] = useState<'idle' | 'recording' | 'transcribing'>('idle');
  const [seconds, setSeconds] = useState(0);
  const recorder = useRef<MediaRecorder | null>(null);
  const stream = useRef<MediaStream | null>(null);
  const cancelled = useRef(false);
  const chunks = useRef<Blob[]>([]);
  const request = useRef<AbortController | null>(null);
  const startedAt = useRef(0);
  const callbacks = useRef({ onTranscript, onError });
  callbacks.current = { onTranscript, onError };
  const release = () => {
    stream.current?.getTracks().forEach((track) => track.stop());
    stream.current = null;
  };
  const cancel = () => {
    cancelled.current = true;
    request.current?.abort();
    if (recorder.current?.state === 'recording') recorder.current.stop();
    release();
    setState('idle');
  };
  useEffect(
    () => () => {
      cancelled.current = true;
      request.current?.abort();
      if (recorder.current?.state === 'recording') recorder.current.stop();
      stream.current?.getTracks().forEach((track) => track.stop());
    },
    [],
  );
  useEffect(() => {
    if (state !== 'recording') return;
    const timer = window.setInterval(() => setSeconds((s) => s + 1), 1000);
    return () => clearInterval(timer);
  }, [state]);
  useEffect(() => {
    if (state === 'recording' && seconds >= maxDuration && recorder.current?.state === 'recording')
      recorder.current.stop();
  }, [seconds, state, maxDuration]);
  const start = async () => {
    if (!navigator.mediaDevices?.getUserMedia || typeof MediaRecorder === 'undefined') {
      callbacks.current.onError(
        'Microphone recording requires a supported browser and HTTPS (or localhost).',
      );
      return;
    }
    cancelled.current = false;
    try {
      const media = await navigator.mediaDevices.getUserMedia({ audio: true });
      if (cancelled.current) {
        media.getTracks().forEach((track) => track.stop());
        return;
      }
      stream.current = media;
      chunks.current = [];
      const mime = [
        'audio/webm;codecs=opus',
        'audio/webm',
        'audio/mp4',
        'audio/ogg;codecs=opus',
      ].find((type) => MediaRecorder.isTypeSupported(type));
      const device = new MediaRecorder(media, mime ? { mimeType: mime } : undefined);
      recorder.current = device;
      device.ondataavailable = (event) => {
        if (event.data.size) chunks.current.push(event.data);
      };
      device.onerror = () => {
        release();
        setState('idle');
        callbacks.current.onError(
          'The browser could not record audio. Check microphone permissions.',
        );
      };
      device.onstop = async () => {
        release();
        if (cancelled.current) return;
        const audio = new Blob(chunks.current, { type: device.mimeType || 'audio/webm' });
        if (audio.size > maxUploadMb * 1024 * 1024) {
          callbacks.current.onError(
            `This recording exceeds the ${maxUploadMb} MB upload limit. Record a shorter message.`,
          );
          setState('idle');
          return;
        }
        setState('transcribing');
        const control = new AbortController();
        request.current = control;
        try {
          const result = await endpoints.transcribe(
            audio,
            control.signal,
            Math.min(maxDuration, (Date.now() - startedAt.current) / 1000),
          );
          if (!cancelled.current) callbacks.current.onTranscript(result.text);
        } catch (error) {
          if (!cancelled.current)
            callbacks.current.onError(
              error instanceof Error
                ? error.message
                : 'Transcription failed. You can continue with text.',
            );
        } finally {
          if (!cancelled.current) setState('idle');
        }
      };
      startedAt.current = Date.now();
      device.start(250);
      setSeconds(0);
      setState('recording');
    } catch (error) {
      release();
      setState('idle');
      callbacks.current.onError(
        error instanceof DOMException && error.name === 'NotAllowedError'
          ? 'Microphone access was denied. Allow microphone access in your browser to record.'
          : 'Microphone recording could not start. Check your audio device.',
      );
    }
  };
  return {
    state,
    seconds,
    start,
    stop: () => {
      if (recorder.current?.state === 'recording') recorder.current.stop();
    },
    cancel,
  };
}

import { useCallback, useEffect, useState } from 'react';
import type { Preferences } from '../types';
export interface SpeechProvider {
  speak(text: string): void;
  stop(): void;
  pause(): void;
  resume(): void;
}
export function useSpeech(preferences: Preferences) {
  const [voices, setVoices] = useState<SpeechSynthesisVoice[]>([]);
  const [speaking, setSpeaking] = useState(false);
  const [paused, setPaused] = useState(false);
  const supported = typeof window !== 'undefined' && 'speechSynthesis' in window;
  useEffect(() => {
    if (!supported) return;
    const refresh = () => setVoices(window.speechSynthesis.getVoices());
    refresh();
    window.speechSynthesis.addEventListener('voiceschanged', refresh);
    return () => {
      window.speechSynthesis.removeEventListener('voiceschanged', refresh);
      window.speechSynthesis.cancel();
    };
  }, [supported]);
  const stop = useCallback(() => {
    if (supported) window.speechSynthesis.cancel();
    setSpeaking(false);
    setPaused(false);
  }, [supported]);
  const speak = useCallback(
    (text: string) => {
      if (!supported || !preferences.speechEnabled) return;
      window.speechSynthesis.cancel();
      const utterance = new SpeechSynthesisUtterance(
        text.replace(/```[\s\S]*?```/g, '').replace(/[#*_`]/g, ''),
      );
      utterance.rate = preferences.speechRate;
      utterance.voice = voices.find((v) => v.voiceURI === preferences.speechVoice) || null;
      utterance.onend = () => {
        setSpeaking(false);
        setPaused(false);
      };
      utterance.onerror = utterance.onend;
      setSpeaking(true);
      setPaused(false);
      window.speechSynthesis.speak(utterance);
    },
    [supported, preferences.speechEnabled, preferences.speechRate, preferences.speechVoice, voices],
  );
  return {
    supported,
    speaking,
    paused,
    voices,
    speak,
    stop,
    pause: () => {
      if (supported) {
        window.speechSynthesis.pause();
        setPaused(true);
      }
    },
    resume: () => {
      if (supported) {
        window.speechSynthesis.resume();
        setPaused(false);
      }
    },
  };
}

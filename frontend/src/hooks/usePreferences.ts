import { useEffect, useState } from 'react';
import type { Preferences } from '../types';
const defaults: Preferences = {
  theme: 'system',
  speechEnabled: true,
  autoRead: false,
  speechVoice: '',
  speechRate: 1,
  autoSendTranscript: false,
};
export function usePreferences() {
  const [preferences, setPreferences] = useState<Preferences>(() => {
    try {
      const saved = JSON.parse(localStorage.getItem('finance-ui-preferences') || '{}');
      return {
        ...defaults,
        ...saved,
        theme: ['light', 'dark', 'system'].includes(saved.theme) ? saved.theme : 'system',
      };
    } catch {
      return defaults;
    }
  });
  useEffect(() => {
    try {
      localStorage.setItem('finance-ui-preferences', JSON.stringify(preferences));
    } catch {
      /* Browser storage may be restricted. */
    }
    const media = window.matchMedia('(prefers-color-scheme: dark)');
    const apply = () =>
      document.documentElement.classList.toggle(
        'dark',
        preferences.theme === 'dark' || (preferences.theme === 'system' && media.matches),
      );
    apply();
    media.addEventListener('change', apply);
    return () => media.removeEventListener('change', apply);
  }, [preferences]);
  return {
    preferences,
    updatePreferences: (changes: Partial<Preferences>) =>
      setPreferences((p) => ({ ...p, ...changes })),
  };
}

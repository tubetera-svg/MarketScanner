// Browser-side alert tones, shared by the scanner page and the global
// price-alert notifier (components/PriceAlertNotifier.tsx).

type AudioContextWindow = Window & { webkitAudioContext?: typeof AudioContext };
let audioContext: AudioContext | null = null;

const playTones = (frequencies: number[], toneSeconds: number, gapSeconds: number) => {
  const Context = window.AudioContext ?? (window as AudioContextWindow).webkitAudioContext;
  if (!Context) return;
  audioContext = audioContext ?? new Context();
  if (audioContext.state === "suspended") void audioContext.resume();
  const startAt = audioContext.currentTime + 0.05;
  frequencies.forEach((frequency, index) => {
    const context = audioContext as AudioContext;
    const start = startAt + index * (toneSeconds + gapSeconds);
    const oscillator = context.createOscillator();
    const gain = context.createGain();
    oscillator.type = "sine";
    oscillator.frequency.value = frequency;
    gain.gain.setValueAtTime(0.0001, start);
    gain.gain.exponentialRampToValueAtTime(0.3, start + 0.02);
    gain.gain.exponentialRampToValueAtTime(0.0001, start + toneSeconds);
    oscillator.connect(gain);
    gain.connect(context.destination);
    oscillator.start(start);
    oscillator.stop(start + toneSeconds);
  });
};

// Browser-side replacement for the scanner's terminal Ring04.wav alert.
export const playAlertSound = (urgent: boolean) => {
  try {
    if (urgent) playTones([988, 1319, 988, 1319], 0.16, 0.07);
    else playTones([784, 1047], 0.4, 0.15);
  } catch {
    // Audio is best-effort; never break scanning over it.
  }
};

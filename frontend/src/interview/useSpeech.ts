/**
 * T-46：把语音能力（识别 + 合成）从 App.tsx 拆成独立 hook。
 *
 * 一个必须说明的行为修正（拆分时顺手修掉，见 docs/31 §5）：
 * `SpeechRecognition` 实例只在挂载时创建一次，它的 `onresult` 回调**跨渲染存活**。
 * 修复前该回调直接引用首次渲染的 `sendMessage` —— 那个闭包里的 `input` 还是空串，
 * 于是 `sendMessage` 第一行 `if (!input.trim()) return;` 把这次提交吞掉，
 * 语音输入只会"填进输入框"、永远不会自动发送。
 * 现在统一走 `onTranscriptRef`（每次渲染刷新），识别结果一定交给**最新**的提交入口。
 */

import { useEffect, useRef, useState } from 'react';

export interface SpeechApi {
  enableSpeech: boolean;
  setEnableSpeech: (value: boolean) => void;
  isListening: boolean;
  speechSupported: boolean;
  startListening: () => void;
  speakText: (text: string) => void;
}

interface UseSpeechOptions {
  /** 识别出文字的落点（写输入框 + 触发提交）；调用方保证传入的是最新渲染的闭包。 */
  onTranscript: (text: string) => void;
}

export function useSpeech({ onTranscript }: UseSpeechOptions): SpeechApi {
  const [enableSpeech, setEnableSpeech] = useState(false);
  const recognitionRef = useRef<any>(null);
  const [isListening, setIsListening] = useState(false);
  const [speechSupported, setSpeechSupported] = useState(true);
  /** 跨渲染存活的识别回调只认这个 ref，不认首次渲染的闭包。 */
  const onTranscriptRef = useRef(onTranscript);
  // 在 effect 里刷新（而不是渲染期直接赋值）：渲染期写 ref 会踩
  // `react-hooks/refs`（"Cannot access refs during render"），而且 effect 一定
  // 早于任何用户交互，识别回调拿到的仍是最新闭包。
  useEffect(() => {
    onTranscriptRef.current = onTranscript;
  });

  // 语音识别初始化
  useEffect(() => {
    if ('webkitSpeechRecognition' in window || 'SpeechRecognition' in window) {
      const SpeechRecognition = (window as any).webkitSpeechRecognition || (window as any).SpeechRecognition;
      recognitionRef.current = new SpeechRecognition();
      recognitionRef.current.continuous = false;
      recognitionRef.current.interimResults = false;
      recognitionRef.current.lang = 'zh-CN';
      recognitionRef.current.onresult = (event: any) => {
        const transcript = event.results[0][0].transcript;
        setIsListening(false);
        onTranscriptRef.current(transcript);
      };
      recognitionRef.current.onerror = () => setIsListening(false);
      recognitionRef.current.onend = () => setIsListening(false);
    } else {
      setSpeechSupported(false);
    }
    // 识别实例只建一次；回调走 ref，因此这里不需要把依赖写全。
  }, []);

  // 语音合成
  const speakText = (text: string) => {
    if (!enableSpeech || !window.speechSynthesis) return;
    const utterance = new SpeechSynthesisUtterance(text);
    utterance.lang = 'zh-CN';
    utterance.rate = 0.9;

    const trySetVoice = () => {
      const voices = window.speechSynthesis.getVoices();
      if (voices.length) {
        const preferred = voices.find(v => v.lang.includes('zh-CN') && (v.name.includes('Tingting') || v.name.includes('Huihui') || v.name.includes('Xiaoxiao') || v.name.includes('Google')));
        if (preferred) utterance.voice = preferred;
        window.speechSynthesis.cancel();
        window.speechSynthesis.speak(utterance);
      } else {
        setTimeout(trySetVoice, 100);
      }
    };
    trySetVoice();
  };

  const startListening = () => {
    if (recognitionRef.current && !isListening) {
      recognitionRef.current.start();
      setIsListening(true);
    }
  };

  return { enableSpeech, setEnableSpeech, isListening, speechSupported, startListening, speakText };
}

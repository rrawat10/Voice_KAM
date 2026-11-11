import React, { useEffect, useRef, useState } from 'react';
import { createRoot } from 'react-dom/client';
import { ArrowUpRight, AudioLines, Check, ChevronRight, CircleHelp, Database, Download, FileSpreadsheet, Headphones, Info, LoaderCircle, Mic, MicOff, RefreshCw, Send, ShieldCheck, Square, Upload, X } from 'lucide-react';
import { VoiceConnection } from './audio';
import './style.css';
import './activity.css';

async function api(path, options) {
  const response = await fetch(path, options);
  if (!response.ok) { const data = await response.json().catch(() => ({})); const error = new Error(typeof data.detail === 'string' ? data.detail : 'The request could not be completed.'); error.status = response.status; throw error; }
  return response.json();
}
const formatNumber = (value, unit) => value == null ? '—' : `${unit === 'INR' ? '₹' : ''}${new Intl.NumberFormat('en-IN', { maximumFractionDigits: 2 }).format(value)}${unit === 'percent' ? '%' : ''}`;
const stageLabels = { idle: 'Ready when you are', connecting: 'Connecting to Mira', listening: 'Listening to you', thinking: 'Checking the context', speaking: 'Mira is speaking', disconnected: 'Conversation paused' };

function recentTurns(history) {
  const starts = [], seen = new Set();
  history.forEach((entry, index) => {
    if (entry.role === 'user' && !seen.has(entry.turn)) { starts.push(index); seen.add(entry.turn); }
  });
  return starts.length > 15 ? history.slice(starts[starts.length - 15]) : starts.length ? history : history.slice(-15);
}

function App() {
  const [session, setSession] = useState(null), [health, setHealth] = useState(null);
  const [busy, setBusy] = useState(false), [error, setError] = useState(''), [stage, setStage] = useState('idle');
  const [muted, setMuted] = useState(false), [level, setLevel] = useState(0), [playback, setPlayback] = useState(false);
  const [turns, setTurns] = useState([]), [timing, setTiming] = useState({}), [text, setText] = useState('');
  const [activity, setActivity] = useState('Choose your data to get started.'), [help, setHelp] = useState(false), [panel, setPanel] = useState('insights');
  const [notice, setNotice] = useState('');
  const [trail, setTrail] = useState([]);
  const voice = useRef(null), eventSource = useRef(null), fileInput = useRef(null), transcript = useRef(null);
  const connected = ['connecting', 'listening', 'thinking', 'speaking'].includes(stage);

  function updateTranscript(entry) {
    setTurns(previous => {
      const list = [...previous];
      const index = list.findIndex(t => t.turn === entry.turn && t.role === entry.role);
      if (index >= 0) list[index] = entry; else list.push(entry);
      return recentTurns(list);
    });
  }
  function openEvents() {
    eventSource.current?.close();
    const source = new EventSource('/api/events'); eventSource.current = source;
    source.onmessage = event => {
      if (eventSource.current !== source) return;
      const { type, data } = JSON.parse(event.data);
      if (type === 'snapshot') {
        setSession(data);
        if (data.history?.length) setTurns(previous => {
          const list = [...data.history];
          const latestTurn = Math.max(...list.map(t => t.turn));
          for (const entry of previous) if (entry.turn >= latestTurn && !list.some(t => t.turn === entry.turn && t.role === entry.role)) list.push(entry);
          return recentTurns(list);
        });
      }
      if (type === 'transcript') updateTranscript(data);
      if (type === 'turn_start') { setTrail([]); setTiming({}); }
      if (type === 'timing') setTiming(previous => ({ ...previous, ...data }));
      if (type === 'status') { setStage(data.stage === 'ready' ? 'listening' : data.stage); setActivity(data.message); if (data.stage === 'thinking') setTrail(previous => [...previous.filter(item => item !== data.message), data.message].slice(-5)); }
      if (type === 'error') setError(data.message);
      if (type === 'ended') { source.close(); voice.current?.close(); setSession(null); setTurns([]); setTiming({}); setStage('idle'); setActivity('Choose data to start fresh.'); if (data.reason === 'expired') setNotice('Session expired after inactivity. Load your data to start again.'); }
    };
    source.onerror = () => { setActivity('Reconnecting the activity feed…'); api('/api/session').catch(e => { if (e.status === 410 || e.status === 401) { source.close(); voice.current?.close(); setSession(null); setTurns([]); setStage('idle'); setNotice('Your session expired or was cleared. Load your data to start again.'); } }); };
  }
  useEffect(() => {
    api('/api/health').then(setHealth).catch(() => setError('The server is unavailable. Please retry shortly.'));
    api('/api/session').then(data => { setSession(data); setTurns(data.history || []); openEvents(); }).catch(e => { if (e.status === 410) setNotice(e.message); });
    return () => { voice.current?.close(); eventSource.current?.close(); };
  }, []);
  useEffect(() => { transcript.current?.scrollTo({ top: transcript.current.scrollHeight, behavior: 'smooth' }); }, [turns]);

  async function chooseData(file) {
    setBusy(true); setError(''); setNotice('');
    try {
      voice.current?.close();
      eventSource.current?.close();
      const options = { method: 'POST' };
      if (file) { const form = new FormData(); form.append('file', file); options.body = form; }
      const data = await api(`/api/sessions/${file ? 'upload' : 'mock'}`, options);
      setSession(data); setTurns([]); setTiming({}); setTrail([]); setStage('idle'); setPanel('data');
      setActivity(file ? 'Spreadsheet validated. Review the values before starting.' : 'Assignment data loaded. Review the values before starting.');
      openEvents();
    } catch (e) { setError(e.message); }
    finally { setBusy(false); if (fileInput.current) fileInput.current.value = ''; }
  }
  async function clearSession() {
    setBusy(true); setError(''); setNotice('');
    voice.current?.close(); voice.current = null;
    eventSource.current?.close(); eventSource.current = null;
    try {
      await api('/api/session', { method: 'DELETE' });
      setSession(null); setTurns([]); setTiming({}); setTrail([]); setStage('idle'); setPanel('insights');
      setActivity('Choose mock data or upload a sheet to start a new conversation.');
    } catch (e) { setError(e.message); }
    finally { setBusy(false); }
  }
  async function start() {
    setError(''); setMuted(false); setPanel('insights');
    voice.current?.close();
    voice.current = new VoiceConnection({ onStatus: setStage, onError: setError, onLevel: setLevel, onPlayback: setPlayback, onTiming: data => setTiming(previous => ({ ...previous, ...data })) });
    await voice.current.connect();
  }
  const facts = Object.values(session?.facts || {}), plan = session?.plan;
  const status = playback ? 'speaking' : stage;
  const buttonDisabled = !session || busy || !health?.voice_configured;

  return <div className="app-shell">
    <header className="topbar">
      <a className="brand" href="/" aria-label="Snapdeal Seller Studio home"><span className="brand-mark"><AudioLines size={22}/></span><span>snap<span className="brand-light">deal</span><span className="brand-dot"> seller studio</span></span></a>
      <nav><span className="nav-active">Voice workspace</span><button onClick={() => setHelp(true)}>How it works <ArrowUpRight size={14}/></button></nav>
      <span className="session-badge"><span className="tiny-dot"/> Private session <span className="badge-divider">/</span> No sign-up</span>
    </header>

    <main>
      <div className="page-heading"><div><p className="eyebrow">SNAPDEAL SELLER ACCOUNT MANAGER</p><h1>Let’s talk business<span>.</span></h1><p className="subtitle">A conversation that turns your numbers into a next step.</p></div><button className="secondary reset-button" disabled={!session || busy} onClick={clearSession}><RefreshCw size={14}/> Clear &amp; restart</button></div>
      {error && <div role="alert" className="notice error"><Info size={18}/><span>{error}</span><button aria-label="Dismiss error" onClick={() => setError('')}><X size={16}/></button></div>}
      {notice && <div role="status" className="notice setup"><Info size={17}/><span>{notice}</span><button aria-label="Dismiss notice" onClick={() => setNotice('')}><X size={16}/></button></div>}
      {health && !health.voice_configured && <div className="notice setup"><Info size={17}/><span><strong>Voice setup pending.</strong> Add a Gemini API key on the server to enable conversations. You can explore mock data and upload Excel now.</span></div>}
      <div className="workspace">
        <section className="conversation-card">
          <div className="card-topline"><span className="section-label"><Headphones size={16}/> YOUR CONVERSATION</span><span className="language-pill" title="Gemini Live detects the language automatically">English · हिन्दी · Hinglish · 99 languages</span></div>
          <div className={`voice-space ${status}`}>
            <div className="orb-wrap" style={{ '--level': level }}><div className="orb-ring ring-one"/><div className="orb-ring ring-two"/><div className="orb"><div className="orb-highlight"/><div className="voice-bars">{[0, 1, 2, 3, 4].map(i => <i key={i} style={{ '--i': i }}/>)}</div></div><span className="orb-spark spark-one">✦</span><span className="orb-spark spark-two">✧</span></div>
            <div className="voice-intro"><div className="name-line">Meet Mira <span>YOUR VOICE KAM</span></div><h2>{stage === 'idle' ? 'Your numbers have a story.' : stageLabels[status] || 'Listening to you'}</h2><p>{stage === 'idle' ? 'Let’s figure it out, together. Start with your data, then tell me what’s on your mind.' : muted ? 'Your microphone is muted. You can still listen or type.' : 'Take your time. You can interrupt, clarify, or change your mind.'}</p></div>
            <div className="voice-controls">{connected ? <><button className="primary" onClick={() => { voice.current?.close(); setStage('disconnected'); }}><Square size={16}/> Pause conversation</button><button className={`icon-button ${muted ? 'muted' : ''}`} aria-label={muted ? 'Unmute microphone' : 'Mute microphone'} onClick={() => setMuted(voice.current?.toggleMute())}>{muted ? <MicOff size={19}/> : <Mic size={19}/>}</button></> : <button className="primary" disabled={buttonDisabled} onClick={start}><Mic size={18}/>{stage === 'disconnected' ? 'Reconnect with Mira' : 'Start conversation'}<ArrowUpRight size={17}/></button>}</div>
            <div className="voice-hint">{!session ? 'Choose mock data or upload a sheet to begin' : !health?.voice_configured ? 'Data is ready · waiting for voice configuration' : <><span className={`tiny-dot ${connected ? 'live' : ''}`}/>{connected ? 'Live audio · up to 5 minutes per connection' : 'Speak naturally in your preferred language'}</>}</div>
          </div>
          <div className="transcript-header"><span className="section-label">CONVERSATION</span><span>{timing.response_ms != null ? `${(timing.response_ms / 1000).toFixed(1)}s response` : 'Live transcript'}</span></div>
          <div className="transcript" ref={transcript} aria-live="polite" aria-label="Conversation transcript">{turns.length ? turns.map((turn, i) => <div className={`turn ${turn.role}`} key={`${turn.role}-${turn.turn}-${i}`}><span className="turn-avatar">{turn.role === 'user' ? 'Y' : 'M'}</span><div><strong>{turn.role === 'user' ? 'You' : 'Mira'}</strong><p>{turn.text}</p></div></div>) : <div className="transcript-empty"><AudioLines size={22}/><div><strong>Space for a real conversation.</strong><p>Your words and Mira’s responses will appear here.</p></div></div>}</div>
          <form className="text-correction" onSubmit={e => { e.preventDefault(); if (text.trim() && connected) { voice.current.text(text.trim()); setText(''); } }}><input aria-label="Type a correction or message" placeholder="Prefer to type a correction?" value={text} maxLength={2000} onChange={e => setText(e.target.value)} disabled={!connected || stage === 'connecting'}/><button aria-label="Send correction" disabled={!connected || !text.trim() || stage === 'connecting'}><Send size={17}/></button></form>
        </section>

        <aside className="context-column">
          <section className="data-source-card"><div className="source-heading"><div><span className="section-label">01 / THE STARTING POINT</span><h3>Your business data</h3></div><FileSpreadsheet size={22}/></div><p>Bring a sheet, or explore the assignment’s sample numbers.</p><div className="source-actions"><button className={`secondary ${session?.dataset.source === 'assignment mock data' ? 'selected' : ''}`} disabled={busy || connected} onClick={() => chooseData()}>{busy ? <LoaderCircle size={16} className="spin"/> : <Database size={16}/>}Use mock data</button><button className="secondary" disabled={busy || connected} onClick={() => fileInput.current.click()}><Upload size={16}/>Upload Excel</button><input ref={fileInput} type="file" accept=".xlsx" hidden onChange={e => e.target.files[0] && chooseData(e.target.files[0])}/></div><a className="template-link" href="/api/template"><Download size={13}/> Download the Excel template <span>.xlsx · up to 2 MB</span></a></section>
          <section className="context-card">
            <div className="panel-tabs"><button className={panel === 'insights' ? 'active' : ''} onClick={() => setPanel('insights')}>Live understanding <span>{facts.length}</span></button><button className={panel === 'data' ? 'active' : ''} onClick={() => setPanel('data')}>Data preview</button></div>
            {panel === 'data' ? <div className="data-panel">{session ? <><div className="source-line"><span className="source-chip"><Check size={12}/>{session.dataset.source === 'assignment mock data' ? 'Assignment mock data' : 'Uploaded spreadsheet'}</span>{session.dataset.filename && <span title={session.dataset.filename} className="filename">{session.dataset.filename}</span>}</div><div className="table-scroll"><table><thead><tr><th>Metric</th><th>Last month</th><th>This month</th></tr></thead><tbody>{session.dataset.metrics.map(m => <tr key={m.id}><td>{m.label}</td><td>{formatNumber(m.previous, m.unit)}</td><td>{formatNumber(m.current, m.unit)}</td></tr>)}</tbody></table></div>{session.dataset.warnings.map(w => <p className="data-warning" key={w}><Info size={14}/>{w}</p>)}</> : <Empty icon={<FileSpreadsheet size={25}/>} title="A clear starting point" text="Choose a data source above. You’ll see every imported value here before you start."/>}</div> : <div className="insight-panel"><div className="insight-section"><div className="section-number">02</div><div className="insight-content"><h3>What I’ve understood <span>SELLER REPORTED</span></h3>{facts.length ? <ul className="fact-list">{facts.map(f => <li key={f.key}><Check size={14}/><div>{f.text}<small>“{f.quote}”</small></div></li>)}</ul> : <p className="empty-copy">Important details from your answers will settle here as we talk.</p>}</div></div><div className="insight-section"><div className="section-number">03</div><div className="insight-content"><h3>What the data suggests</h3>{plan?.findings?.length ? plan.findings.map((f, i) => <div className="finding" key={i}><span className={`status-chip ${f.status}`}>{f.status === 'possible' ? 'Possible explanation' : 'Data observation'}</span><p>{f.text}</p><Evidence ids={f.evidence_ids} session={session}/></div>) : <p className="empty-copy">Observations will be tied to your data. A possible cause stays a possibility.</p>}</div></div><div className="insight-section"><div className="section-number">04</div><div className="insight-content"><h3>Your next steps</h3>{plan?.recommendations?.length ? plan.recommendations.map((r, i) => <div className="recommendation" key={i}><span>{i + 1}</span><div><strong>{r.title}</strong><p>{r.detail}</p><Evidence ids={r.evidence_ids} session={session}/></div></div>) : <p className="empty-copy">Practical recommendations come after we understand the situation.</p>}</div></div>{(plan?.unknowns?.length > 0) && <div className="open-questions"><span className="section-label"><CircleHelp size={14}/> STILL TO UNDERSTAND</span>{plan?.unknowns?.map((q, i) => <p key={i}>{q}</p>)}</div>}</div>}
            {!!Object.values(session?.dataset?.evidence || {}).filter(item => item.source === 'Python calculation').length && <div className="calculation-list"><span className="section-label">CALCULATED DURING THIS CONVERSATION</span>{Object.entries(session.dataset.evidence).filter(([, c]) => c.source === 'Python calculation').map(([id, c]) => <div className="calculation" key={id}><strong>{c.label}: {formatNumber(c.value, c.unit)}</strong><p>{c.operation.replaceAll('_', ' ')} · inputs {c.input_values.join(' → ')}</p><small>{c.qualification}</small></div>)}</div>}
            {session?.summary && <details className="activity-trail"><summary>Conversation summary</summary><p style={{ whiteSpace: 'pre-wrap' }}>{session.summary}</p></details>}
            {trail.length > 0 && <details className="activity-trail"><summary>Activity this turn <span>{trail.length} operations</span></summary><ol>{trail.map((item, i) => <li key={i}>{item}</li>)}</ol></details>}
            <div className="activity"><span className={`tiny-dot ${connected ? 'live' : ''}`}/><span>{activity}</span>{stage === 'thinking' && <LoaderCircle size={13} className="spin"/>}</div>
          </section>
          <div className="privacy-note"><ShieldCheck size={16}/><span>No account. Session data expires after 30 minutes of inactivity.</span></div>
        </aside>
      </div>
      <footer><span>Built for thoughtful seller conversations.<span className="footer-divider">·</span>{health?.storage || 'Connecting to server…'}</span></footer>
    </main>
    {help && <div className="modal-backdrop" onClick={() => setHelp(false)}><section className="help-modal" role="dialog" aria-modal="true" aria-label="How it works" onClick={e => e.stopPropagation()}><button className="modal-close" onClick={() => setHelp(false)} aria-label="Close help"><X size={20}/></button><span className="eyebrow">A CONVERSATION, GROUNDED IN DATA</span><h2>Small steps.<br/>A clearer picture.</h2><ol><li><strong>Start with your numbers.</strong> Load the sample or an Excel table with Metric, Last Month and This Month columns.</li><li><strong>Tell Mira what’s happening.</strong> Speak in English, Hindi, Hinglish, or another supported language. She uses your answers and the data to ask relevant follow-ups.</li><li><strong>Leave with a next step.</strong> Review what’s known, what’s uncertain, and what you could do next.</li></ol><p>Gemini Live detects the spoken language automatically and can switch naturally in a conversation. This is a prototype: hypotheses are not proven causes, and recommendations do not guarantee results. Voice requires a configured Gemini key. Best experienced in Chrome or Edge with headphones.</p><button className="primary" onClick={() => setHelp(false)}>Back to the workspace <ChevronRight size={16}/></button></section></div>}
  </div>;
}
function Empty({ icon, title, text }) { return <div className="empty-state">{icon}<h3>{title}</h3><p>{text}</p></div>; }
function Evidence({ ids, session }) { return <div className="evidence">{ids.map(id => <span key={id} title={id}>{id.startsWith('seller.') ? 'Seller stated' : session?.dataset.evidence[id]?.label || id}</span>)}</div>; }
createRoot(document.getElementById('root')).render(<App/>);

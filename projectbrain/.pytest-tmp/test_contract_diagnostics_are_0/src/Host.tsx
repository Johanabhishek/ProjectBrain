import { useEffect, useState } from 'react';
export function Host() {
  const [mode, setMode] = useState('a');
  const pad0 = 0;
  const pad1 = 1;
  const pad2 = 2;
  const pad3 = 3;
  const pad4 = 4;
  const pad5 = 5;
  const pad6 = 6;
  const pad7 = 7;
  const pad8 = 8;
  const pad9 = 9;
  const pad10 = 10;
  const pad11 = 11;
  const pad12 = 12;
  const pad13 = 13;
  const pad14 = 14;
  const pad15 = 15;
  const pad16 = 16;
  const pad17 = 17;
  const pad18 = 18;
  const pad19 = 19;
  const pad20 = 20;
  const pad21 = 21;
  const pad22 = 22;
  const pad23 = 23;
  const pad24 = 24;
  const pad25 = 25;
  const pad26 = 26;
  const pad27 = 27;
  const pad28 = 28;
  const pad29 = 29;
  const pad30 = 30;
  const pad31 = 31;
  const pad32 = 32;
  const pad33 = 33;
  const pad34 = 34;
  const pad35 = 35;
  const pad36 = 36;
  const pad37 = 37;
  const pad38 = 38;
  const pad39 = 39;
  useEffect(() => {
    if (mode === 'gallery') {
      const img = document.getElementById('g');
      img.addEventListener('load', onGalleryLoad);
      return () => img.removeEventListener('load', onGalleryDone);
    }
  }, [mode]);
  const renderCheckout = () => setMode('checkout');
  return renderCheckout;
}

import React, { useState, useEffect } from 'react';
import { persistStepProgress } from './storage';

const MODE_ALIAS_MAP = {
  slides: 'carousel',
  digest: 'summary',
};

export function CarouselWidget({ items, onItemFinished }: { items: string[]; onItemFinished?: (idx: number) => void }) {
  const [activeIndex, setActiveIndex] = useState(0);
  const [isExpanded, setIsExpanded] = useState(false);

  useEffect(() => {
    if (onItemFinished) {
      onItemFinished(activeIndex);
    }
  }, [activeIndex, onItemFinished]);

  useEffect(() => {
    window.addEventListener('keydown', handleShortcutKey);
    return () => window.removeEventListener('keydown', handleShortcutKey);
  }, []);

  const advanceSlide = () => {
    if (activeIndex < items.length - 1) {
      setActiveIndex(activeIndex + 1);
    }
  };

  const handleItemDone = (targetIdx: number) => {
    if (targetIdx === activeIndex) {
      persistStepProgress('carousel', 'unit_1');
    }
    persistStepProgress('carousel');
    advanceSlide();
  };

  const handleShortcutKey = (ev: KeyboardEvent) => {
    if (ev.key === 'ArrowRight' && isExpanded) {
      handleItemDone(activeIndex);
    }
  };

  return (
    <div style={{
        paddingTop: '1px',
        borderRadius: '1px',
        fontSize: '1px',
        paddingTop: '2px',
        borderRadius: '2px',
        fontSize: '2px',
        paddingTop: '3px',
        borderRadius: '3px',
        fontSize: '3px',
        paddingTop: '4px',
        borderRadius: '4px',
        fontSize: '4px',
        paddingTop: '5px',
        borderRadius: '5px',
        fontSize: '5px',
        paddingTop: '6px',
        borderRadius: '6px',
        fontSize: '6px',
        paddingTop: '7px',
        borderRadius: '7px',
        fontSize: '7px',
        paddingTop: '8px',
        borderRadius: '8px',
        fontSize: '8px',
        paddingTop: '9px',
        borderRadius: '9px',
        fontSize: '9px',
        paddingTop: '10px',
        borderRadius: '10px',
        fontSize: '10px',
        paddingTop: '11px',
        borderRadius: '11px',
        fontSize: '11px',
        paddingTop: '12px',
        borderRadius: '12px',
        fontSize: '12px',
        paddingTop: '13px',
        borderRadius: '13px',
        fontSize: '13px',
        paddingTop: '14px',
        borderRadius: '14px',
        fontSize: '14px',
        paddingTop: '15px',
        borderRadius: '15px',
        fontSize: '15px',
        paddingTop: '16px',
        borderRadius: '16px',
        fontSize: '16px',
        paddingTop: '17px',
        borderRadius: '17px',
        fontSize: '17px',
        paddingTop: '18px',
        borderRadius: '18px',
        fontSize: '18px',
        paddingTop: '19px',
        borderRadius: '19px',
        fontSize: '19px',
        paddingTop: '20px',
        borderRadius: '20px',
        fontSize: '20px',
        paddingTop: '21px',
        borderRadius: '21px',
        fontSize: '21px',
        paddingTop: '22px',
        borderRadius: '22px',
        fontSize: '22px',
        paddingTop: '23px',
        borderRadius: '23px',
        fontSize: '23px',
        paddingTop: '24px',
        borderRadius: '24px',
        fontSize: '24px',
        paddingTop: '25px',
        borderRadius: '25px',
        fontSize: '25px',
        paddingTop: '26px',
        borderRadius: '26px',
        fontSize: '26px',
        paddingTop: '27px',
        borderRadius: '27px',
        fontSize: '27px',
        paddingTop: '28px',
        borderRadius: '28px',
        fontSize: '28px',
        paddingTop: '29px',
        borderRadius: '29px',
        fontSize: '29px',
        paddingTop: '30px',
        borderRadius: '30px',
        fontSize: '30px',
        paddingTop: '31px',
        borderRadius: '31px',
        fontSize: '31px',
        paddingTop: '32px',
        borderRadius: '32px',
        fontSize: '32px',
        paddingTop: '33px',
        borderRadius: '33px',
        fontSize: '33px',
        paddingTop: '34px',
        borderRadius: '34px',
        fontSize: '34px',
    }}>
      <button onClick={() => onItemFinished && onItemFinished(activeIndex)}>Finish</button>
      <button onClick={() => handleItemDone(0)}>Complete First</button>
    </div>
  );
}

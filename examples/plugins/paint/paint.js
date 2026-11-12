export async function mount(root, sdk) {
  const toolbar = document.createElement('div');toolbar.className='row';
  const color = document.createElement('input');color.type='color';color.value='#0866d9';color.setAttribute('aria-label','Brush color');
  const clear=document.createElement('button');clear.textContent='Clear drawing';
  const save=document.createElement('button');save.textContent='Save drawing';
  const canvas=document.createElement('canvas');canvas.width=1000;canvas.height=620;canvas.className='paint-canvas';canvas.setAttribute('aria-label','Drawing canvas');
  const context=canvas.getContext('2d');context.lineWidth=4;context.lineCap='round';
  const hint=document.createElement('p');hint.textContent='Draw with your mouse or touch. Save keeps the drawing for this bot.';hint.className='hint';
  toolbar.append(color,clear,save);root.append(toolbar,canvas,hint);
  let drawing=false;
  const point=e=>{const b=canvas.getBoundingClientRect();return [(e.clientX-b.left)*canvas.width/b.width,(e.clientY-b.top)*canvas.height/b.height];};
  canvas.addEventListener('pointerdown',e=>{canvas.setPointerCapture(e.pointerId);drawing=true;context.strokeStyle=color.value;context.beginPath();context.moveTo(...point(e));},{signal:sdk.signal});
  canvas.addEventListener('pointermove',e=>{if(drawing){context.lineTo(...point(e));context.stroke();}},{signal:sdk.signal});
  for(const event of ['pointerup','pointercancel'])canvas.addEventListener(event,()=>{drawing=false;},{signal:sdk.signal});
  clear.onclick=()=>context.clearRect(0,0,canvas.width,canvas.height);
  save.onclick=async()=>{save.disabled=true;try{await sdk.storage.set('drawing',canvas.toDataURL('image/png'));sdk.toast('Drawing saved.');}catch(error){sdk.toast(error.message,true);}finally{save.disabled=false;}};
  const saved=await sdk.storage.get('drawing');
  if(saved&&!sdk.signal.aborted){const image=new Image();image.onload=()=>{if(!sdk.signal.aborted)context.drawImage(image,0,0);};image.src=saved;}
}

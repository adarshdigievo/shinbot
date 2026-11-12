export function activate(sdk) {
  const button=document.createElement('button');button.type='button';button.className='quiet small-button';button.textContent='Review';
  button.onclick=()=>sdk.compose('Review the current changes. Check correctness, risks and missing tests.');
  return sdk.mount('composer-actions',button);
}

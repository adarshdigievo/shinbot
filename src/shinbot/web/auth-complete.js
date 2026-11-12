(() => {
  const title=document.querySelector('#auth-title'), status=document.querySelector('#auth-status');
  const params=new URLSearchParams(location.search), attempt=params.get('id');
  const failed=params.get('result')==='failed'||!attempt;
  history.replaceState(null,'',location.pathname);
  function show(heading,message){title.textContent=heading;status.textContent=message;document.title=heading+' · ShinBot';}
  if(failed){show('Sign-in could not finish','This attempt expired or was cancelled. Return to ShinBot to start again.');return;}
  const deadline=Date.now()+60000;
  async function check(){
    try {
      const response=await fetch('/api/login');
      if(!response.ok){show('Return to ShinBot','Your callback was received. Open your original ShinBot tab to check the connection.');return;}
      const result=await response.json();
      if(result.id!==attempt){show('Return to ShinBot','This sign-in was replaced. Continue with the current attempt in ShinBot.');return;}
      if(result.status==='complete'){show('Account connected','You can close this tab and continue in ShinBot.');return;}
      if(result.status!=='pending'){show('Sign-in could not finish',result.error||'Return to ShinBot and start a new sign-in.');return;}
      if(Date.now()<deadline){setTimeout(check,1000);return;}
      show('Still connecting','Return to ShinBot to check the connection.');
    } catch {show('Connection interrupted','Return to ShinBot to check your connection and try again.');}
  }
  check();
})();

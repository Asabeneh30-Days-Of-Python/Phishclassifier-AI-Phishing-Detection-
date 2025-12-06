// static/js/monitor-export-clicks.js
document.addEventListener('click', function(e){
  const a = e.target.closest('a');
  if(!a) return;
  const href = a.getAttribute('href') || '';
  if(href.includes('format=csv') || href.includes('format=json')){
    console.info('EXPORT CLICK:', href);
    const payload = JSON.stringify({ href });
    // prefer sendBeacon for navigation-safe logging
    if (navigator && navigator.sendBeacon) {
      try {
        navigator.sendBeacon('/__log_export_click', payload);
        return;
      } catch (err) {
        // fall back to fetch
      }
    }
    const headers = {'Content-Type':'application/json'};
    const csrfMeta = document.querySelector('meta[name="csrf-token"]');
    if (csrfMeta) headers['X-CSRFToken'] = csrfMeta.getAttribute('content');
    fetch('/__log_export_click', {method:'POST', headers, body: payload}).catch(()=>{/* non-blocking */});
  }
});

// static/js/websocket.js
const socket = io('http://localhost:5000', {
  transports: ['websocket', 'polling'],
  path: '/socket.io'
});

socket.on('connect', () => {
  console.log('Socket.IO connected, sid=', socket.id);
});

socket.on('training_update', data => {
  console.log('⇠ training_update', data);
  // find the <li> for this job.id, update its status, etc.
});

socket.on('disconnect', reason => {
  console.warn('Socket.IO disconnected:', reason);
});

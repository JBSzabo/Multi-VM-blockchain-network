const http = require('http');
const fs = require('fs');
const path = require('path');
const port = process.env.WEB_PORT || 3002;

http.createServer((req, res) => {
    fs.readFile(path.join(__dirname, 'index.html'), (err, data) => {
        if (err) { res.writeHead(500); return res.end('Error loading page'); }
        res.writeHead(200, { 'Content-Type': 'text/html' });
        res.end(data);
    });
}).listen(port, '0.0.0.0', () => console.log(`Web UI listening on ${port}`));

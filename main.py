from flask import Flask, Blueprint, request, Response, jsonify, render_template_string
import requests
import re
from urllib.parse import urljoin, urlparse, parse_qs, urlencode, quote, unquote
from bs4 import BeautifulSoup
import base64
import logging
import chardet
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)

# Create a session with retry strategy
def create_session():
    session = requests.Session()
    retry_strategy = Retry(
        total=3,
        backoff_factor=1,
        status_forcelist=[429, 500, 502, 503, 504],
    )
    adapter = HTTPAdapter(max_retries=retry_strategy)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session

def is_valid_url(url):
    """Check if URL is valid and safe to proxy"""
    try:
        if not url or not isinstance(url, str):
            return False
        
        # Skip problematic URL types
        problematic_prefixes = ['blob:', 'data:', 'javascript:', 'mailto:', 'tel:', 'file:']
        if any(url.lower().startswith(prefix) for prefix in problematic_prefixes):
            return False
        
        parsed = urlparse(url)
        
        # Must have valid scheme and netloc
        if parsed.scheme not in ['http', 'https'] or not parsed.netloc:
            return False
            
        # Reject URLs with credentials for security
        if parsed.username or parsed.password:
            return False
            
        return True
    except Exception:
        return False

def get_base_url(url):
    """Get base URL for relative URL resolution"""
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}"

def make_absolute_url(base_url, relative_url):
    """Convert relative URL to absolute URL"""
    if not relative_url:
        return base_url
    return urljoin(base_url, relative_url)

def encode_url(url):
    """Safely encode URL for proxy routing"""
    return base64.urlsafe_b64encode(url.encode('utf-8')).decode('utf-8')

def decode_url(encoded_url):
    """Safely decode URL from proxy routing"""
    try:
        return base64.urlsafe_b64decode(encoded_url.encode('utf-8')).decode('utf-8')
    except Exception:
        return None

def rewrite_html_content(html_content, original_url, proxy_base):
    """Rewrite HTML content to work with proxy"""
    try:
        # Use html5lib parser which handles encoding better
        soup = BeautifulSoup(html_content, 'html5lib')
        base_url = get_base_url(original_url)
        
        # Ensure UTF-8 encoding
        if soup.head:
            # Remove existing charset meta tags
            for meta in soup.head.find_all('meta'):
                if meta.get('charset') or (meta.get('http-equiv') and 'content-type' in meta.get('http-equiv', '').lower()):
                    meta.decompose()
            
            # Add proper UTF-8 charset
            charset_meta = soup.new_tag('meta', charset='utf-8')
            soup.head.insert(0, charset_meta)
        
        # Handle base tag
        base_tag = soup.find('base')
        if base_tag and base_tag.get('href'):
            base_url = make_absolute_url(original_url, base_tag['href'])
        
        # Rewrite links
        for tag in soup.find_all('a', href=True):
            original_href = tag['href']
            if not original_href.startswith(('javascript:', '#', 'mailto:', 'tel:')):
                absolute_url = make_absolute_url(base_url, original_href)
                if is_valid_url(absolute_url):
                    tag['href'] = f"{proxy_base}/browse?url={quote(absolute_url)}"
        
        # Rewrite forms
        for form in soup.find_all('form'):
            if form.get('action'):
                absolute_url = make_absolute_url(base_url, form['action'])
                if is_valid_url(absolute_url):
                    form['action'] = f"{proxy_base}/browse"
                    # Add hidden field with target URL
                    hidden_input = soup.new_tag('input', type='hidden', name='_proxy_url', value=absolute_url)
                    form.insert(0, hidden_input)
        
        # Rewrite resources (images, scripts, stylesheets)
        resource_tags = [
            ('img', 'src'),
            ('script', 'src'),
            ('link', 'href'),
            ('source', 'src'),
            ('iframe', 'src')
        ]
        
        for tag_name, attr in resource_tags:
            for tag in soup.find_all(tag_name):
                if tag.get(attr):
                    absolute_url = make_absolute_url(base_url, tag[attr])
                    if is_valid_url(absolute_url):
                        encoded_url = encode_url(absolute_url)
                        tag[attr] = f"{proxy_base}/resource/{encoded_url}"
        
        # Inject proxy JavaScript
        proxy_script = f"""
        <script>
        (function() {{
            const PROXY_BASE = '{proxy_base}';
            
            // Override form submissions
            document.addEventListener('submit', function(e) {{
                const form = e.target;
                if (form.method.toLowerCase() === 'post') {{
                    e.preventDefault();
                    
                    const formData = new FormData(form);
                    const targetUrl = formData.get('_proxy_url') || form.action;
                    
                    if (targetUrl && targetUrl !== PROXY_BASE + '/browse') {{
                        // Convert FormData to regular object
                        const data = {{}};
                        for (let [key, value] of formData.entries()) {{
                            if (key !== '_proxy_url') {{
                                data[key] = value;
                            }}
                        }}
                        
                        // Submit via fetch
                        fetch(PROXY_BASE + '/browse', {{
                            method: 'POST',
                            headers: {{
                                'Content-Type': 'application/x-www-form-urlencoded',
                            }},
                            body: new URLSearchParams({{
                                url: targetUrl,
                                ...data
                            }})
                        }})
                        .then(response => response.text())
                        .then(html => {{
                            document.open();
                            document.write(html);
                            document.close();
                        }})
                        .catch(console.error);
                    }}
                }}
            }});
            
            // Handle navigation
            function navigateTo(url) {{
                if (url && !url.startsWith('javascript:') && !url.startsWith('#')) {{
                    window.location.href = PROXY_BASE + '/browse?url=' + encodeURIComponent(url);
                }}
            }}
            
            // Override window.open
            const originalOpen = window.open;
            window.open = function(url, name, features) {{
                if (url && !url.startsWith('javascript:')) {{
                    const absoluteUrl = new URL(url, window.location.href).href;
                    return originalOpen(PROXY_BASE + '/browse?url=' + encodeURIComponent(absoluteUrl), name, features);
                }}
                return originalOpen(url, name, features);
            }};
            
        }})();
        </script>
        """
        
        # Insert script before closing body tag
        if soup.body:
            soup.body.append(BeautifulSoup(proxy_script, 'html.parser'))
        else:
            soup.append(BeautifulSoup(proxy_script, 'html.parser'))
        
        return str(soup)
    except Exception as e:
        logging.error(f"Error rewriting HTML: {e}")
        return html_content

def rewrite_css_content(css_content, original_url, proxy_base):
    """Rewrite CSS content to work with proxy"""
    try:
        # Find all url() references in CSS
        url_pattern = r'url\s*\(\s*["\']?([^"\')\s]+)["\']?\s*\)'
        
        def replace_url(match):
            url = match.group(1).strip()
            if url.startswith(('data:', '#')):
                return match.group(0)
            
            absolute_url = make_absolute_url(original_url, url)
            if is_valid_url(absolute_url):
                encoded_url = encode_url(absolute_url)
                return f'url("{proxy_base}/resource/{encoded_url}")'
            return match.group(0)
        
        return re.sub(url_pattern, replace_url, css_content)
    except Exception as e:
        logging.error(f"Error rewriting CSS: {e}")
        return css_content

@app.route('/')
def home():
    """Serve the main proxy interface"""
    html_template = """
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>🔒 SecureProxy - Anonymous Web Browser</title>
        <style>
            * { margin: 0; padding: 0; box-sizing: border-box; }
            body {
                font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
                background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                min-height: 100vh;
                display: flex;
                flex-direction: column;
            }
            .header {
                background: rgba(255, 255, 255, 0.1);
                backdrop-filter: blur(10px);
                padding: 1rem 0;
                box-shadow: 0 2px 20px rgba(0,0,0,0.1);
            }
            .container {
                max-width: 1200px;
                margin: 0 auto;
                padding: 0 2rem;
            }
            .logo {
                font-size: 2rem;
                font-weight: bold;
                color: white;
                margin-bottom: 0.5rem;
            }
            .tagline {
                color: rgba(255, 255, 255, 0.8);
                font-size: 1.1rem;
            }
            .nav-bar {
                background: white;
                border-radius: 15px;
                padding: 1rem;
                margin: 2rem 0;
                box-shadow: 0 10px 30px rgba(0,0,0,0.1);
            }
            .url-form {
                display: flex;
                gap: 1rem;
                margin-bottom: 1rem;
            }
            .url-input {
                flex: 1;
                padding: 0.75rem 1rem;
                border: 2px solid #e1e5e9;
                border-radius: 8px;
                font-size: 1rem;
                transition: border-color 0.3s;
            }
            .url-input:focus {
                outline: none;
                border-color: #667eea;
            }
            .browse-btn {
                background: #667eea;
                color: white;
                border: none;
                padding: 0.75rem 2rem;
                border-radius: 8px;
                font-size: 1rem;
                cursor: pointer;
                transition: background 0.3s;
            }
            .browse-btn:hover {
                background: #5a6fd8;
            }
            .nav-buttons {
                display: flex;
                gap: 0.5rem;
            }
            .nav-btn {
                background: #f8f9fa;
                border: 1px solid #dee2e6;
                padding: 0.5rem 1rem;
                border-radius: 6px;
                cursor: pointer;
                font-size: 0.9rem;
                transition: background 0.3s;
            }
            .nav-btn:hover {
                background: #e9ecef;
            }
            .content {
                flex: 1;
                background: white;
                margin: 0 2rem 2rem;
                border-radius: 15px;
                padding: 3rem;
                box-shadow: 0 10px 30px rgba(0,0,0,0.1);
            }
            .welcome {
                text-align: center;
                color: #495057;
            }
            .welcome h2 {
                font-size: 2.5rem;
                margin-bottom: 1rem;
                color: #343a40;
            }
            .features {
                display: grid;
                grid-template-columns: repeat(auto-fit, minmax(250px, 1fr));
                gap: 2rem;
                margin-top: 3rem;
            }
            .feature {
                text-align: center;
                padding: 2rem;
                border-radius: 10px;
                background: #f8f9fa;
                transition: transform 0.3s;
            }
            .feature:hover {
                transform: translateY(-5px);
            }
            .feature-icon {
                font-size: 3rem;
                margin-bottom: 1rem;
            }
            .feature h3 {
                margin-bottom: 1rem;
                color: #495057;
            }
        </style>
    </head>
    <body>
        <div class="header">
            <div class="container">
                <div class="logo">🔒 SecureProxy</div>
                <div class="tagline">Browse the web anonymously and bypass restrictions</div>
            </div>
        </div>
        
        <div class="container">
            <div class="nav-bar">
                <form class="url-form" action="/browse" method="get">
                    <input type="text" name="url" class="url-input" placeholder="Enter website URL (e.g., google.com, reddit.com)" required>
                    <button type="submit" class="browse-btn">Browse</button>
                </form>
                <div class="nav-buttons">
                    <button class="nav-btn" onclick="history.back()">← Back</button>
                    <button class="nav-btn" onclick="history.forward()">Forward →</button>
                    <button class="nav-btn" onclick="location.reload()">🔄 Refresh</button>
                    <button class="nav-btn" onclick="location.href='/'">🏠 Home</button>
                </div>
            </div>
            
            <div class="content">
                <div class="welcome">
                    <h2>Ready to browse...</h2>
                    <p>Welcome to SecureProxy</p>
                    <p>Enter any website URL above to browse anonymously. Your traffic will be routed through our secure proxy servers, helping you bypass local network restrictions and maintain privacy.</p>
                    
                    <div class="features">
                        <div class="feature">
                            <div class="feature-icon">🛡️</div>
                            <h3>Anonymous Browsing</h3>
                            <p>Your real IP address is hidden from the websites you visit</p>
                        </div>
                        <div class="feature">
                            <div class="feature-icon">🚀</div>
                            <h3>Bypass Restrictions</h3>
                            <p>Access blocked websites and content from anywhere</p>
                        </div>
                        <div class="feature">
                            <div class="feature-icon">🔒</div>
                            <h3>Secure Connection</h3>
                            <p>All traffic is encrypted and routed through secure servers</p>
                        </div>
                    </div>
                </div>
            </div>
        </div>
    </body>
    </html>
    """
    return render_template_string(html_template)

@app.route('/browse', methods=['GET', 'POST'])
def browse():
    """Main proxy endpoint"""
    session = create_session()
    
    try:
        # Get target URL
        if request.method == 'POST':
            target_url = request.form.get('url') or request.form.get('_proxy_url', '')
            form_data = {k: v for k, v in request.form.items() if k not in ['url', '_proxy_url']}
        else:
            target_url = request.args.get('url', '').strip()
            form_data = {}
        
        if not target_url:
            return "URL is required", 400
        
        # Add protocol if missing
        if not target_url.startswith(('http://', 'https://')):
            target_url = 'https://' + target_url
        
        if not is_valid_url(target_url):
            return f"Invalid or unsupported URL: {target_url}", 400
        
        # Set up headers
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.5',
            'Accept-Encoding': 'gzip, deflate, br',
            'Connection': 'keep-alive',
        }
        
        # Make request
        if request.method == 'POST' and form_data:
            response = session.post(target_url, headers=headers, data=form_data, timeout=30, allow_redirects=True)
        else:
            response = session.get(target_url, headers=headers, timeout=30, allow_redirects=True)
        
        # Get proxy base URL
        proxy_base = request.url_root.rstrip('/')
        
        # Handle response based on content type
        content_type = response.headers.get('content-type', '').lower()
        
        if 'text/html' in content_type:
            # Detect encoding
            detected_encoding = chardet.detect(response.content)
            encoding = detected_encoding.get('encoding', 'utf-8')
            
            # Decode with detected encoding
            try:
                html_text = response.content.decode(encoding)
            except:
                html_text = response.content.decode('utf-8', errors='ignore')
            
            # Process HTML content
            processed_html = rewrite_html_content(html_text, response.url, proxy_base)
            return Response(processed_html, content_type='text/html; charset=utf-8')
        else:
            # Return other content as-is
            return Response(response.content, content_type=content_type)
            
    except requests.exceptions.Timeout:
        return "Request timed out", 504
    except requests.exceptions.ConnectionError:
        return "Could not connect to the website", 502
    except requests.exceptions.RequestException as e:
        logging.error(f"Request error: {e}")
        return f"Error fetching website: {str(e)}", 500
    except Exception as e:
        logging.error(f"Unexpected error: {e}")
        return "An unexpected error occurred", 500

@app.route('/resource/<encoded_url>')
def proxy_resource(encoded_url):
    """Proxy static resources like images, CSS, JS"""
    session = create_session()
    
    try:
        target_url = decode_url(encoded_url)
        if not target_url or not is_valid_url(target_url):
            return "Invalid resource URL", 400
        
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        }
        
        response = session.get(target_url, headers=headers, timeout=15)
        
        content_type = response.headers.get('content-type', '').lower()
        proxy_base = request.url_root.rstrip('/')
        
        # Process CSS files to rewrite URLs
        if 'text/css' in content_type:
            # Detect encoding for CSS
            detected_encoding = chardet.detect(response.content)
            encoding = detected_encoding.get('encoding', 'utf-8')
            
            try:
                css_text = response.content.decode(encoding)
            except:
                css_text = response.content.decode('utf-8', errors='ignore')
            
            processed_css = rewrite_css_content(css_text, target_url, proxy_base)
            return Response(processed_css, content_type='text/css; charset=utf-8')
        else:
            # Return other resources as-is
            return Response(response.content, content_type=content_type)
            
    except Exception as e:
        logging.error(f"Resource proxy error: {e}")
        return "Error loading resource", 404

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False)

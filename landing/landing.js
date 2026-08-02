/* landing.js — Landing page interactions (extracted from inline <script> for CSP nonce compliance) */

// ─── Scroll Reveal ───
const revealEls = document.querySelectorAll('.reveal');
const revealObserver = new IntersectionObserver((entries) => {
  entries.forEach(e => { if (e.isIntersecting) { e.target.classList.add('visible'); revealObserver.unobserve(e.target); } });
}, { threshold: 0.1, rootMargin: '0px 0px -40px 0px' });
revealEls.forEach(el => revealObserver.observe(el));

// ─── Animated Counters ───
const counters = document.querySelectorAll('.counter');
const counterObserver = new IntersectionObserver((entries) => {
  entries.forEach(e => {
    if (e.isIntersecting) {
      const el = e.target;
      const target = parseInt(el.dataset.target);
      const prefix = el.dataset.prefix || '';
      const suffix = el.dataset.suffix || '';
      let current = 0;
      const step = Math.ceil(target / 60);
      const interval = setInterval(() => {
        current += step;
        if (current >= target) { current = target; clearInterval(interval); }
        el.textContent = prefix + current.toLocaleString() + suffix;
      }, 25);
      counterObserver.unobserve(el);
    }
  });
}, { threshold: 0.5 });
counters.forEach(c => counterObserver.observe(c));

// ─── Nav Sticky ───
const nav = document.getElementById('nav');
window.addEventListener('scroll', () => {
  nav.classList.toggle('scrolled', window.scrollY > 40);
});

// ─── Mobile Menu ───
const navToggle = document.getElementById('navToggle');
const mobileMenu = document.getElementById('mobileMenu');
const mobileClose = document.getElementById('mobileClose');
navToggle.addEventListener('click', () => mobileMenu.classList.add('open'));
mobileClose.addEventListener('click', () => mobileMenu.classList.remove('open'));
function closeMenu() { mobileMenu.classList.remove('open'); }

// Close mobile menu when any menu link is clicked (CSP-safe: no inline onclick)
document.querySelectorAll('.mobile-menu-link').forEach(link => {
  link.addEventListener('click', closeMenu);
});

// ─── Accordion ───
function toggleAccordion(btn) {
  const item = btn.closest('.accordion-item');
  const body = item.querySelector('.accordion-body');
  const inner = body.querySelector('.accordion-body-inner');
  const isOpen = item.classList.contains('open');

  // Close all
  document.querySelectorAll('.accordion-item').forEach(i => {
    i.classList.remove('open');
    i.querySelector('.accordion-body').style.maxHeight = '0';
  });

  // Toggle current
  if (!isOpen) {
    item.classList.add('open');
    body.style.maxHeight = inner.scrollHeight + 40 + 'px';
  }
}

// Attach accordion handlers via addEventListener (CSP-safe: no inline onclick)
document.querySelectorAll('.accordion-header').forEach(btn => {
  btn.addEventListener('click', () => toggleAccordion(btn));
});

// ─── Form ───
async function handleSubmit(e) {
  e.preventDefault();
  const form = e.target;
  const data = Object.fromEntries(new FormData(form));
  const btn = form.querySelector('button[type="submit"]');
  if (btn) btn.disabled = true;
  try {
    const res = await fetch('/api/v1/leads', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(data),
    });
    if (res.ok) {
      alert('¡Gracias ' + (data.name || '') + '! Nos pondremos en contacto contigo pronto.');
      form.reset();
    } else {
      const err = await res.json().catch(() => ({}));
      alert('Error al enviar: ' + (err.detail || 'Intenta de nuevo.'));
    }
  } catch (err) {
    alert('Error de conexión. Intenta de nuevo más tarde.');
  } finally {
    if (btn) btn.disabled = false;
  }
}

// Attach form handler via addEventListener (replaces inline onsubmit for CSP compliance)
document.addEventListener('DOMContentLoaded', function() {
  const ctaForm = document.querySelector('.cta-form');
  if (ctaForm) {
    ctaForm.addEventListener('submit', handleSubmit);
  }
});

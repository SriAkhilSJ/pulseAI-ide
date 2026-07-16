import React from "react";
import "./App.css";

const products = [
  {
    name: "Nord Sofa",
    price: "$499",
    description: "Modern minimal sofa.",
  },
  {
    name: "Nord Chair",
    price: "$199",
    description: "Sleek wooden chair.",
  },
  {
    name: "Nord Table",
    price: "$299",
    description: "Compact coffee table.",
  },
  {
    name: "Nord Lamp",
    price: "$99",
    description: "Ambient lighting lamp.",
  },
];

function App() {
  return (
    <div className="app">
      {/* Navigation Bar */}
      <nav className="navbar">
        <div className="logo">Nord</div>
        <ul className="nav-links">
          <li>Shop</li>
          <li>About</li>
          <li>Contact</li>
        </ul>
      </nav>

      {/* Hero Section */}
      <section className="hero">
        <h1>Elevate Your Space</h1>
        <p>Fine furniture for modern living.</p>
        <button className="cta-button">Shop Now</button>
      </section>

      {/* Featured Products */}
      <section className="featured">
        <h2>Featured Products</h2>
        <div className="product-grid">
          {products.map((p, idx) => (
            <div className="product-card" key={idx}>
              <div className="placeholder" />
              <h3>{p.name}</h3>
              <p className="price">{p.price}</p>
              <p>{p.description}</p>
            </div>
          ))}
        </div>
      </section>

      {/* Footer */}
      <footer className="footer">© 2024 Nord Furniture. All rights reserved.</footer>
    </div>
  );
}

export default App;

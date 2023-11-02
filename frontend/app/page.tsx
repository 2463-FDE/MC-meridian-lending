export default function Home() {
  return (
    <main className="wrap">
      <h1>Meridian Lending</h1>
      <p className="sub">Consumer loan origination &amp; servicing platform</p>

      <div className="card">
        <p>
          Welcome to the Meridian Lending portal. Borrowers can apply for a personal
          installment loan and manage their account here.
        </p>
        <p className="sub" style={{ marginTop: 16 }}>
          <span className="badge">SOX-controlled</span>{" "}
          <span className="badge">PCI compliant</span>{" "}
          <span className="badge">ECOA / Reg B</span>
        </p>
        <h2>Get started</h2>
        <p>
          <a href="/apply">Apply for a loan →</a>
          <br />
          <a href="/servicing">Servicing dashboard →</a>
        </p>
      </div>
    </main>
  );
}

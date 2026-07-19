# PCI DSS Compliance Guide for Archisynapse

## Executive Summary

| Level | Transactions/Year | Assessment Type | First-Year Cost | Annual Maintenance |
|-------|-------------------|-----------------|-----------------|-------------------|
| Level 1 | >6M | Full QSA Audit | $70K-$200K | $50K-$150K |
| Level 2 | 1M-6M | SAQ + QSA Oversight | $30K-$80K | $20K-$40K |
| Level 3 | 20K-1M | SAQ Self-Assessment | $15K-$35K | $10K-$20K |
| Level 4 | <20K | SAQ A (Simple) | $8K-$20K | $5K-$10K |

**Archisynapse Target: Start at Level 3-4, scale to Level 1**

---

## PCI DSS 4.0 Requirements (Effective March 2025)

### The 12 Core Requirements

| # | Requirement | What It Means for Archisynapse |
|---|-------------|-------------------------------|
| 1 | Install and maintain network security controls | Firewalls, network segmentation |
| 2 | Apply secure configurations | Change default passwords, disable unnecessary services |
| 3 | Protect stored account data | Encrypt cardholder data at rest |
| 4 | Protect cardholder data in transit | TLS 1.2+ for all transmissions |
| 5 | Protect against malicious software | Anti-virus, malware detection |
| 6 | Develop and maintain secure systems | Secure coding, vulnerability management |
| 7 | Restrict access by business need-to-know | Role-based access control |
| 8 | Identify users and authenticate access | Multi-factor authentication |
| 9 | Restrict physical access to cardholder data | Physical security controls |
| 10 | Log and monitor all access | Audit trails, SIEM integration |
| 11 | Test security systems regularly | Vulnerability scans, penetration testing |
| 12 | Maintain an information security policy | Documented policies, training |

### Key Changes in 4.0.1

1. **Continuous Monitoring** - Security controls must be sustained year-round, not just at audit time
2. **Enhanced Encryption** - Stricter requirements for encryption standards
3. **Stricter Access Controls** - More granular permission management
4. **Mandatory Penetration Testing** - Annual external penetration tests required
5. **Evidence Collection** - More documentation and monitoring expectations

---

## Compliance Strategy for Archisynapse

### Phase 1: Descope (Months 1-3)

**Strategy: Use tokenization to reduce PCI scope**

```typescript
// GOOD: Card data never touches our servers
// Use Stripe Elements, Adyen Drop-in, or similar

// BAD: Card data passes through our servers
// This triggers SAQ D (most expensive)
```

**Architecture Decision:**
```
┌─────────────────────────────────────────────────────────┐
│  Customer Browser                                       │
│    ↓ (card data)                                        │
│  Stripe Elements (PCI-compliant iframe)                 │
│    ↓ (token only)                                       │
│  Archisynapse Transaction Service                       │
│    ↓ (token)                                            │
│  Stripe API (handles actual card processing)            │
└─────────────────────────────────────────────────────────┘
```

**Result:** SAQ A compliance ($8K-$20K) instead of SAQ D ($40K-$80K)

### Phase 2: Security Foundation (Months 3-6)

1. **Network Segmentation**
   - Isolate cardholder data environment
   - Implement firewall rules
   - Separate production from development

2. **Access Control**
   - Multi-factor authentication for all admin access
   - Role-based access control (RBAC)
   - Principle of least privilege

3. **Encryption**
   - TLS 1.2+ for all communications
   - AES-256 for data at rest
   - Key management procedures

4. **Logging & Monitoring**
   - Centralized log collection (SIEM)
   - Real-time alerting
   - 1-year log retention minimum

### Phase 3: Assessment Preparation (Months 6-9)

1. **Gap Analysis**
   - Self-assessment questionnaire
   - Internal vulnerability scan
   - Documentation review

2. **Remediation**
   - Fix identified gaps
   - Implement missing controls
   - Update policies and procedures

3. **Penetration Testing**
   - External network scan
   - Internal network scan
   - Application security testing

### Phase 4: Certification (Months 9-12)

1. **QSA Engagement**
   - Select Qualified Security Assessor
   - Scope definition
   - Evidence collection

2. **Assessment**
   - On-site or remote assessment
   - Control testing
   - Report generation

3. **Attestation**
   - Sign Attestation of Compliance (AoC)
   - Submit to acquiring bank
   - Maintain compliance program

---

## Cost Breakdown for Archisynapse

### First-Year Investment

| Category | Low Estimate | High Estimate | Notes |
|----------|--------------|---------------|-------|
| QSA Assessment | $20,000 | $80,000 | Depends on scope |
| Penetration Testing | $10,000 | $25,000 | External + internal |
| Security Tooling | $15,000 | $40,000 | SIEM, WAF, etc. |
| Engineering Time | $50,000 | $150,000 | Implementation |
| Documentation | $5,000 | $15,000 | Policies, procedures |
| **Total** | **$100,000** | **$310,000** | |

### Annual Recurring Costs

| Category | Annual Cost |
|----------|-------------|
| QSA Re-assessment | $15,000 - $50,000 |
| Penetration Testing | $10,000 - $25,000 |
| Security Tooling | $10,000 - $30,000 |
| Training | $2,000 - $5,000 |
| **Total** | **$37,000 - $110,000** |

### ROI Calculation

**Without PCI DSS:**
- Potential breach cost: $5.85M average
- Monthly fines: $5,000 - $100,000
- Loss of processing capability: Existential

**With PCI DSS:**
- First-year cost: ~$150K-$200K
- Annual maintenance: ~$50K-$70K
- **Payback:** One prevented breach saves $5M+

---

## Architecture Recommendations

### 1. Tokenization First

```typescript
// Use Stripe or similar for card handling
const stripe = require('stripe')(process.env.STRIPE_SECRET_KEY);

// Create payment method - card data never touches our servers
const paymentMethod = await stripe.paymentMethods.create({
  type: 'card',
  card: {
    token: 'tok_xxx' // From Stripe Elements
  }
});

// Store only the token, not the card
await db.query(
  'INSERT INTO payment_methods (user_id, stripe_payment_method_id) VALUES ($1, $2)',
  [userId, paymentMethod.id]
);
```

### 2. Network Segmentation

```
┌─────────────────────────────────────────────────────────┐
│  Public Zone (DMZ)                                      │
│  - API Gateway                                          │
│  - Load Balancer                                        │
├─────────────────────────────────────────────────────────┤
│  Application Zone                                       │
│  - Transaction Service                                  │
│  - Ledger Service                                       │
├─────────────────────────────────────────────────────────┤
│  Data Zone (CDE)                                        │
│  - PostgreSQL (encrypted)                               │
│  - Redis (encrypted)                                    │
└─────────────────────────────────────────────────────────┘
```

### 3. Logging Architecture

```typescript
// Centralized logging with audit trail
const winston = require('winston');

const logger = winston.createLogger({
  level: 'info',
  format: winston.format.json(),
  transports: [
    new winston.transports.File({ filename: 'audit.log' }),
    new winston.transports.Http({ host: 'siem.internal' })
  ]
});

// Log all access to sensitive data
app.use((req, res, next) => {
  logger.info({
    timestamp: new Date().toISOString(),
    userId: req.user?.id,
    action: req.method,
    path: req.path,
    ip: req.ip,
    userAgent: req.headers['user-agent']
  });
  next();
});
```

---

## Compliance Checklist

### Immediate (Week 1-2)
- [ ] Use hosted payment page (Stripe Elements)
- [ ] Enable TLS 1.2+ everywhere
- [ ] Implement basic access control
- [ ] Set up logging

### Short-term (Month 1-3)
- [ ] Network segmentation
- [ ] Multi-factor authentication
- [ ] Encryption at rest
- [ ] Vulnerability scanning

### Medium-term (Month 3-6)
- [ ] Penetration testing
- [ ] Incident response plan
- [ ] Security awareness training
- [ ] Policy documentation

### Long-term (Month 6-12)
- [ ] QSA engagement
- [ ] Full assessment
- [ ] Attestation of Compliance
- [ ] Ongoing monitoring program

---

## Resources

- [PCI Security Standards Council](https://www.pcisecuritystandards.org/)
- [PCI DSS 4.0 Quick Reference Guide](https://www.pcisecuritystandards.org/document_library/)
- [Stripe PCI Compliance Guide](https://stripe.com/docs/security/guide)
- [PCI DSS Cost Calculator](https://datavirtualizer.com/pci-compliance-cost-calculator/)

---

*Last Updated: January 2026*
*Status: Planning Phase*

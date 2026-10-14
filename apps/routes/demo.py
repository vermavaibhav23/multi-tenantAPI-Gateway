"""Static demonstration data, not business API implementations."""
INDUSTRY_BUNDLES = {
    'ecommerce': ('E-commerce', (
        ('User Service', 'fashion-user-service', '/users'),
        ('Product Service', 'fashion-product-service', '/products'),
        ('Order Service', 'fashion-order-service', '/orders'),
    )),
    'healthcare': ('Healthcare', (
        ('Doctor Service', 'healthcare-doctor-service', '/doctors'),
        ('Appointment Service', 'healthcare-appointment-service', '/appointments'),
        ('Patient Service', 'healthcare-patient-service', '/patients'),
    )),
    'banking': ('Banking', (
        ('Account Service', 'banking-account-service', '/accounts'),
        ('Transaction Service', 'banking-transaction-service', '/transactions'),
        ('Payment Service', 'banking-payment-service', '/payments'),
    )),
}

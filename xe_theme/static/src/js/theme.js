let SwiperMarquee = new Swiper('.swiper-marquee', {
    spaceBetween: 16,
    centeredSlides: true,
    speed: 6000,
    autoplay: {
      delay: 1,
      disableOnInteraction: true,
    },
    loop: true,
    slidesPerView: 'auto',
    allowTouchMove: false,
});